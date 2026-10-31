"""Gateway configuration.

Configuration has two sources:

* Process environment (``GATEWAY_*`` variables) for deployment settings and gateway
  credentials, loaded into ``Settings``.
* A TOML file, referenced by ``GATEWAY_CONFIG_FILE``, describing upstream providers
  and model aliases. The file never contains secrets; each provider names the
  environment variable that holds its API key.

Everything is validated at startup so that a misconfigured gateway fails to start
rather than failing on the first request.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from gateway.errors import ConfigurationError

MIN_PRODUCTION_API_KEY_LENGTH = 32

ProviderName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ModelAlias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]
EnvVarName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", frozen=True, hide_input_in_errors=True)

    environment: Environment = Environment.PRODUCTION
    api_keys: Annotated[tuple[SecretStr, ...], NoDecode] = Field(min_length=1)
    config_file: Path = Path("config/gateway.toml")
    log_level: LogLevel = "INFO"
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=64 * 1_048_576)

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(key.strip() for key in value.split(",") if key.strip())
        return value

    @model_validator(mode="after")
    def _check_production_keys(self) -> "Settings":
        if self.environment is Environment.PRODUCTION and any(
            len(key.get_secret_value()) < MIN_PRODUCTION_API_KEY_LENGTH for key in self.api_keys
        ):
            raise ValueError(
                f"gateway API keys must be at least {MIN_PRODUCTION_API_KEY_LENGTH} characters"
                " in production"
            )
        return self


class _ProviderConfigBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: HttpUrl
    api_key_env: EnvVarName | None = None
    allow_insecure_http: bool = False
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=120.0, gt=0)
    write_timeout_seconds: float = Field(default=30.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    max_connections: int = Field(default=100, ge=1)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, url: HttpUrl) -> HttpUrl:
        if url.username or url.password:
            raise ValueError("base_url must not embed credentials; use api_key_env")
        if url.query or url.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        return url


class OpenAICompatibleProviderConfig(_ProviderConfigBase):
    type: Literal["openai_compatible"]


class AnthropicProviderConfig(_ProviderConfigBase):
    type: Literal["anthropic"]
    api_key_env: EnvVarName
    api_version: str = Field(default="2023-06-01", pattern=r"^\d{4}-\d{2}-\d{2}$")


ProviderConfig = Annotated[
    OpenAICompatibleProviderConfig | AnthropicProviderConfig, Field(discriminator="type")
]


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    model: str = Field(min_length=1, max_length=256)
    default_max_tokens: int | None = Field(default=None, ge=1)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: dict[ProviderName, ProviderConfig] = Field(min_length=1)
    models: dict[ModelAlias, ModelConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_model_providers(self) -> "GatewayConfig":
        for alias, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(f"model {alias!r} references unknown provider {model.provider!r}")
        return self


@dataclass(frozen=True)
class LoadedConfig:
    gateway: GatewayConfig
    provider_api_keys: Mapping[str, SecretStr | None]


def load_config(settings: Settings, environ: Mapping[str, str]) -> LoadedConfig:
    """Read and validate the provider configuration file and resolve provider credentials."""
    gateway = _read_gateway_config(settings.config_file)
    if settings.environment is Environment.PRODUCTION:
        _check_production_transport(gateway)
    return LoadedConfig(gateway=gateway, provider_api_keys=_resolve_api_keys(gateway, environ))


def _read_gateway_config(path: Path) -> GatewayConfig:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigurationError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"configuration file {path} is not valid TOML: {exc}") from None

    try:
        return GatewayConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise ConfigurationError(f"invalid configuration file {path}: {details}") from None


def _check_production_transport(gateway: GatewayConfig) -> None:
    for name, provider in gateway.providers.items():
        if provider.base_url.scheme != "https" and not provider.allow_insecure_http:
            raise ConfigurationError(
                f"provider {name!r} uses a non-HTTPS base_url in production;"
                " set allow_insecure_http = true to permit it explicitly"
            )


def _resolve_api_keys(
    gateway: GatewayConfig, environ: Mapping[str, str]
) -> dict[str, SecretStr | None]:
    keys: dict[str, SecretStr | None] = {}
    for name, provider in gateway.providers.items():
        if provider.api_key_env is None:
            keys[name] = None
            continue
        value = environ.get(provider.api_key_env, "").strip()
        if not value:
            raise ConfigurationError(
                f"provider {name!r} requires environment variable {provider.api_key_env}"
            )
        keys[name] = SecretStr(value)
    return keys
