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

import hashlib
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

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

from gateway.cost.config import CostConfig
from gateway.cost.pricing import PriceCatalog
from gateway.errors import ConfigurationError
from gateway.routing.config import AliasRouting, Capability, RoutingConfig, RoutingPolicy
from gateway.schemas.completions import MAX_OUTPUT_TOKENS

MIN_PRODUCTION_API_KEY_LENGTH = 32

ProviderName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ModelAlias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]
EnvVarName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
# The non-secret identifier of a gateway key; see core.security.Principal.
PrincipalId = Annotated[str, Field(pattern=r"^key-[0-9a-f]{12}$")]
# Keeps token-bucket arithmetic well within the exact integer range of the
# double-precision numbers used by Redis Lua scripts.
MAX_RATE_LIMIT = 1_000_000_000
AnthropicApiVersion = Literal["2023-06-01", "2026-10-21"]


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
    # May embed a password, so it is held as a secret and never logged.
    redis_url: SecretStr | None = None
    redis_ca_file: Path | None = None
    database_url: SecretStr | None = None

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(key.strip() for key in value.split(",") if key.strip())
        return value

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, url: SecretStr | None) -> SecretStr | None:
        if url is not None:
            validate_redis_url(url)
        return url

    @model_validator(mode="after")
    def _check_production_keys(self) -> "Settings":
        if self.environment is Environment.PRODUCTION and any(
            len(key.get_secret_value()) < MIN_PRODUCTION_API_KEY_LENGTH for key in self.api_keys
        ):
            raise ValueError(
                f"gateway API keys must be at least {MIN_PRODUCTION_API_KEY_LENGTH} characters"
                " in production"
            )
        if (
            self.environment is Environment.PRODUCTION
            and self.redis_url is not None
            and self.redis_url.get_secret_value().startswith("redis://")
        ):
            raise ValueError("production Redis TCP connections require rediss:// with TLS")
        return self


def validate_redis_url(url: SecretStr) -> None:
    try:
        parsed = urlsplit(url.get_secret_value())
        valid = (
            parsed.scheme in {"redis", "rediss", "unix"}
            and not parsed.query
            and not parsed.fragment
            and (bool(parsed.path) if parsed.scheme == "unix" else bool(parsed.hostname))
        )
        # Accessing port also validates its syntax and range.
        if parsed.port is not None and parsed.scheme == "unix":
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Redis URL must use redis://, rediss://, or unix:// without query or fragment"
        )


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

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Request features the adapter can send to this provider."""
        return frozenset(Capability)


class AnthropicProviderConfig(_ProviderConfigBase):
    type: Literal["anthropic"]
    api_key_env: EnvVarName
    # The adapter only sends request fields it knows each API version accepts, so
    # only versions it has been written against are allowed.
    api_version: AnthropicApiVersion = "2026-10-21"

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Request features the adapter can send to this provider."""
        if self.api_version == "2026-10-21":
            # This API version rejects temperature and top_p.
            return frozenset(Capability) - {Capability.TEMPERATURE, Capability.TOP_P}
        return frozenset(Capability)


ProviderConfig = Annotated[
    OpenAICompatibleProviderConfig | AnthropicProviderConfig, Field(discriminator="type")
]


# Each fallback adds a possible upstream call per request.
MAX_FALLBACKS = 3


class FallbackTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    model: str = Field(min_length=1, max_length=256)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    model: str = Field(min_length=1, max_length=256)
    default_max_tokens: int | None = Field(default=None, ge=1)
    # Requests asking for more are rejected, and requests that do not specify
    # max_tokens are sent this value (unless default_max_tokens is set), so that every
    # request has a known output bound.
    max_output_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    # Tried in order when the primary fails before running the request (see
    # docs/resilience.md). Each names a configured provider and its model ID.
    fallbacks: list[FallbackTarget] = Field(default_factory=list, max_length=MAX_FALLBACKS)
    # Takes effect only when routing.enabled is true; see docs/routing.md.
    routing: AliasRouting = Field(default_factory=AliasRouting)

    @property
    def output_allowance(self) -> int | None:
        """The max_tokens sent when the client does not set it."""
        if self.default_max_tokens is not None:
            return self.default_max_tokens
        return self.max_output_tokens

    def route_targets(self) -> list[tuple[str, str]]:
        """The primary followed by the routing candidates."""
        return [(self.provider, self.model)] + [
            (target.provider, target.model) for target in self.routing.candidates
        ]

    @model_validator(mode="after")
    def _check_output_limits(self) -> "ModelConfig":
        if (
            self.default_max_tokens is not None
            and self.max_output_tokens is not None
            and self.default_max_tokens > self.max_output_tokens
        ):
            raise ValueError("default_max_tokens must not exceed max_output_tokens")
        targets = [(self.provider, self.model)] + [(t.provider, t.model) for t in self.fallbacks]
        if len(set(targets)) != len(targets):
            raise ValueError("fallbacks must not repeat the primary or another fallback")
        if self.routing.policy is not RoutingPolicy.FIXED:
            if self.fallbacks:
                # A routed alias falls back to its other eligible candidates, which
                # satisfy the same policy; separate fallbacks might not.
                raise ValueError("a routed alias uses its candidates instead of fallbacks")
            if (self.provider, self.model) in self.route_targets()[1:]:
                raise ValueError("routing candidates must not repeat the primary")
        return self


RateLimit = Annotated[int, Field(ge=1, le=MAX_RATE_LIMIT)]


class RateLimitsConfig(BaseModel):
    """Limits for one gateway key. An omitted limit is not enforced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests_per_minute: RateLimit | None = None
    # Bucket capacity: how many requests may be made at once after a quiet period.
    # Defaults to requests_per_minute.
    request_burst: RateLimit | None = None
    tokens_per_minute: RateLimit | None = None
    token_burst: RateLimit | None = None
    max_concurrent_requests: Annotated[int, Field(ge=1, le=100_000)] | None = None

    @model_validator(mode="after")
    def _check_bursts(self) -> "RateLimitsConfig":
        if self.request_burst is not None and self.requests_per_minute is None:
            raise ValueError("request_burst requires requests_per_minute")
        if self.token_burst is not None and self.tokens_per_minute is None:
            raise ValueError("token_burst requires tokens_per_minute")
        return self


class AdmissionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    default_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    # Replaces default_limits entirely for the listed keys, identified by the
    # non-secret principal ID that appears in logs (key- plus 12 hex characters).
    key_limits: dict[PrincipalId, RateLimitsConfig] = Field(default_factory=dict)
    # Concurrency leases live for the request deadline plus this grace period, so a
    # lease cannot expire while its request may still be running.
    lease_grace_seconds: float = Field(default=30.0, gt=0, le=600)
    # How long a settled reservation is remembered after its lease, to recognise
    # duplicate settlement.
    reservation_retention_seconds: float = Field(default=300.0, gt=0, le=86_400)
    redis_connect_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    redis_operation_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    redis_pool_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    redis_max_connections: int = Field(default=50, ge=1, le=10_000)

    def limits_for(self, principal_id: str) -> RateLimitsConfig:
        return self.key_limits.get(principal_id, self.default_limits)


# One hour; longer requests are better served by an asynchronous API.
MAX_REQUEST_DEADLINE_SECONDS = 3600


# Upper bound on attempts per provider, to keep retry amplification small.
MAX_ATTEMPTS_PER_PROVIDER = 5
# Statuses that may be configured as retryable, besides 429: server errors.
_RETRYABLE_STATUS_RANGE = range(500, 600)


class CircuitBreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    # The breaker opens after this many provider failures within the window.
    failure_threshold: int = Field(default=5, ge=1, le=1000)
    failure_window_seconds: float = Field(default=30.0, gt=0, le=3600)
    # How long the breaker stays open before letting probe requests through.
    recovery_seconds: float = Field(default=30.0, gt=0, le=3600)
    half_open_max_probes: int = Field(default=1, ge=1, le=100)


class ResilienceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Covers the whole request: admission, every provider attempt and backoff
    # delay, and for streams the time until the last event has been sent.
    request_deadline_seconds: float = Field(default=300.0, gt=0, le=MAX_REQUEST_DEADLINE_SECONDS)
    # 1 disables retries.
    max_attempts_per_provider: int = Field(default=2, ge=1, le=MAX_ATTEMPTS_PER_PROVIDER)
    retry_initial_backoff_seconds: float = Field(default=0.2, gt=0, le=10)
    retry_max_backoff_seconds: float = Field(default=2.0, gt=0, le=60)
    # Each delay is reduced by a random fraction of up to this ratio.
    retry_jitter_ratio: float = Field(default=0.5, ge=0, le=1)
    # A provider asking to wait longer than this is not retried.
    max_retry_after_seconds: float = Field(default=10.0, ge=0, le=300)
    retry_on_status: frozenset[int] = frozenset({429})
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    @field_validator("retry_on_status")
    @classmethod
    def _check_statuses(cls, statuses: frozenset[int]) -> frozenset[int]:
        invalid = sorted(s for s in statuses if s != 429 and s not in _RETRYABLE_STATUS_RANGE)
        if invalid:
            raise ValueError(f"only 429 and 5xx statuses can be retried, not {invalid}")
        return statuses

    @model_validator(mode="after")
    def _check_backoff(self) -> "ResilienceConfig":
        if self.retry_max_backoff_seconds < self.retry_initial_backoff_seconds:
            raise ValueError("retry_max_backoff_seconds must be at least the initial backoff")
        return self


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: dict[ProviderName, ProviderConfig] = Field(min_length=1)
    models: dict[ModelAlias, ModelConfig] = Field(min_length=1)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    admission: AdmissionConfig = Field(default_factory=AdmissionConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)

    @model_validator(mode="after")
    def _check_model_providers(self) -> "GatewayConfig":
        for alias, model in self.models.items():
            for provider in [
                model.provider,
                *(target.provider for target in model.fallbacks),
                *(target.provider for target in model.routing.candidates),
            ]:
                if provider not in self.providers:
                    raise ValueError(f"model {alias!r} references unknown provider {provider!r}")
        self._check_routing()
        if self.cost.enabled:
            catalog = PriceCatalog(self.cost.pricing)
            for alias, model in self.models.items():
                if model.default_max_tokens is None and model.max_output_tokens is None:
                    raise ValueError(f"model {alias!r} needs an output bound for cost accounting")
                for provider, upstream_model in [
                    *model.route_targets(),
                    *((target.provider, target.model) for target in model.fallbacks),
                ]:
                    if not catalog.has_model(provider, upstream_model):
                        raise ValueError(
                            f"model {alias!r} has no price for {provider}/{upstream_model}"
                        )
                    catalog.select(provider, upstream_model, datetime.now(UTC))
        return self

    def _check_routing(self) -> None:
        registry = self.routing.registry()
        for entry in self.routing.models:
            provider_config = self.providers.get(entry.provider)
            if provider_config is None:
                raise ValueError(f"routing.models references unknown provider {entry.provider!r}")
            unsupported = entry.capabilities - provider_config.capabilities
            if unsupported:
                raise ValueError(
                    f"routing.models entry {entry.provider}/{entry.model} lists capabilities"
                    f" its provider adapter does not support: {sorted(unsupported)}"
                )
        for alias, model in self.models.items():
            if model.routing.policy is RoutingPolicy.FIXED:
                continue
            # Cost-based selection needs prices, and a routed request must be
            # reserved against budgets at the selected model's price.
            if not self.cost.enabled:
                raise ValueError(f"model {alias!r} uses dynamic routing, which requires cost")
            for provider, upstream_model in model.route_targets():
                capabilities = registry.get((provider, upstream_model))
                if capabilities is None:
                    raise ValueError(
                        f"model {alias!r} routes to {provider}/{upstream_model},"
                        " which has no routing.models entry"
                    )
                allowance = model.output_allowance
                if allowance is not None and allowance > capabilities.max_output_tokens:
                    raise ValueError(
                        f"model {alias!r} sends max_tokens={allowance} by default, above the"
                        f" limit of {capabilities.max_output_tokens} for"
                        f" {provider}/{upstream_model}"
                    )


@dataclass(frozen=True)
class LoadedConfig:
    gateway: GatewayConfig
    provider_api_keys: Mapping[str, SecretStr | None]


def load_config(settings: Settings, environ: Mapping[str, str]) -> LoadedConfig:
    """Read and validate the provider configuration file and resolve provider credentials."""
    gateway = read_gateway_config(settings.config_file)
    if settings.environment is Environment.PRODUCTION:
        _check_production_transport(gateway)
    if gateway.admission.enabled:
        require_redis_url(settings)
    if gateway.cost.enabled and settings.database_url is None:
        raise ConfigurationError("cost accounting is enabled but GATEWAY_DATABASE_URL is not set")
    if gateway.cost.enabled:
        configured_ids = {
            f"key-{hashlib.sha256(key.get_secret_value().encode()).hexdigest()[:12]}"
            for key in settings.api_keys
        }
        referenced_ids = (
            set(gateway.cost.key_limits)
            | set(gateway.cost.principal_teams)
            | {reader for readers in gateway.cost.team_readers.values() for reader in readers}
        )
        unknown = referenced_ids - configured_ids
        if unknown:
            raise ConfigurationError(
                f"cost configuration references unknown gateway keys: {sorted(unknown)}"
            )
    return LoadedConfig(gateway=gateway, provider_api_keys=resolve_api_keys(gateway, environ))


def require_redis_url(settings: Settings) -> SecretStr:
    if settings.redis_url is None:
        raise ConfigurationError("admission control is enabled but GATEWAY_REDIS_URL is not set")
    return settings.redis_url


def read_gateway_config(path: Path) -> GatewayConfig:
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


def resolve_api_keys(
    gateway: GatewayConfig, environ: Mapping[str, str], names: Collection[str] | None = None
) -> dict[str, SecretStr | None]:
    """Resolve the keys of every provider, or of only ``names``."""
    keys: dict[str, SecretStr | None] = {}
    for name, provider in gateway.providers.items():
        if names is not None and name not in names:
            continue
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
