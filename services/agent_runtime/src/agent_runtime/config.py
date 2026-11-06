"""Agent runtime configuration, read from ``AGENT_*`` environment variables.

The runtime has no configuration file. Everything is validated at startup, so a
misconfigured runtime fails to start instead of failing on its first
investigation. Credentials are held as ``SecretStr`` and never appear in ``repr``
or validation errors.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIN_PRODUCTION_KEY_LENGTH = 32

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


def _split_list(value: object) -> object:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", frozen=True, hide_input_in_errors=True)

    environment: Environment = Environment.PRODUCTION
    # Credentials accepted from callers of the runtime's own API.
    api_keys: Annotated[tuple[SecretStr, ...], NoDecode] = Field(min_length=1)
    log_level: LogLevel = "INFO"
    max_request_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)

    # The gateway, and the gateway API key the runtime presents to it. The key must
    # be listed in the gateway's GATEWAY_API_KEYS, so the gateway's limits and
    # budgets for that key apply to every investigation.
    gateway_url: HttpUrl
    gateway_api_key: SecretStr
    gateway_allow_insecure_http: bool = False
    gateway_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    gateway_read_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    gateway_write_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    gateway_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    gateway_max_connections: int = Field(default=10, ge=1, le=100)
    gateway_max_request_bytes: int = Field(default=262_144, ge=4096, le=1_048_576)
    gateway_max_response_bytes: int = Field(default=262_144, ge=4096, le=4_194_304)

    _split_api_keys = field_validator("api_keys", mode="before")(_split_list)

    @field_validator("gateway_url")
    @classmethod
    def _check_gateway_url(cls, url: HttpUrl) -> HttpUrl:
        if url.username or url.password:
            raise ValueError("gateway_url must not embed credentials; use AGENT_GATEWAY_API_KEY")
        if url.query or url.fragment:
            raise ValueError("gateway_url must not contain a query string or fragment")
        return url

    @model_validator(mode="after")
    def _check_production_keys(self) -> "Settings":
        production = self.environment is Environment.PRODUCTION
        if production and any(
            len(key.get_secret_value()) < MIN_PRODUCTION_KEY_LENGTH
            for key in (*self.api_keys, self.gateway_api_key)
        ):
            raise ValueError(
                f"agent and gateway API keys must be at least {MIN_PRODUCTION_KEY_LENGTH}"
                " characters in production"
            )
        if not self.gateway_api_key.get_secret_value():
            raise ValueError("gateway_api_key must not be empty")
        if self.gateway_api_key.get_secret_value() in {
            key.get_secret_value() for key in self.api_keys
        }:
            # Otherwise every runtime caller could also call the gateway directly.
            raise ValueError("the gateway API key must differ from every agent API key")
        if (
            production
            and self.gateway_url.scheme != "https"
            and not self.gateway_allow_insecure_http
        ):
            raise ValueError(
                "gateway_url must use https in production unless"
                " AGENT_GATEWAY_ALLOW_INSECURE_HTTP is true"
            )
        return self
