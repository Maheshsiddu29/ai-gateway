"""Agent runtime configuration, read from ``AGENT_*`` environment variables.

The runtime has no configuration file. Everything is validated at startup, so a
misconfigured runtime fails to start instead of failing on its first
investigation. Credentials are held as ``SecretStr`` and never appear in ``repr``
or validation errors.
"""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from agent_runtime.diagnostics.tools import validate_allowed_paths
from agent_runtime.investigation.schemas import MAX_PLAN_STEPS

MIN_PRODUCTION_KEY_LENGTH = 32

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
# The gateway's alias syntax.
ModelAlias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


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

    # Diagnostics. The roots exist so tests, or a container with the host's
    # filesystems mounted elsewhere, can point the tools at other locations.
    procfs_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    # The only paths the filesystem tools accept, matched exactly.
    filesystem_paths: Annotated[tuple[str, ...], NoDecode] = ("/",)
    tool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # Threads for diagnostic reads. A read that hangs keeps its thread; when all
    # are taken, further reads fail as diagnostic_workers_busy.
    diagnostic_workers: int = Field(default=4, ge=1, le=16)
    max_tool_output_bytes: int = Field(default=16_384, ge=1024, le=262_144)

    # Investigations. Model aliases are required: the runtime never picks a model.
    planner_model: ModelAlias
    diagnosis_model: ModelAlias
    max_llm_calls: int = Field(default=2, ge=1, le=8)
    max_tool_calls: int = Field(default=6, ge=1, le=MAX_PLAN_STEPS)
    # Sent as max_tokens on every call. Reasoning tokens, where a provider uses
    # them, count toward it, so it must leave room for the JSON reply.
    max_output_tokens: int = Field(default=2048, ge=64, le=16_384)
    investigation_timeout_seconds: float = Field(default=180.0, gt=0, le=900)
    max_concurrent_investigations: int = Field(default=4, ge=1, le=64)

    _split_lists = field_validator("api_keys", "filesystem_paths", mode="before")(_split_list)

    @field_validator("filesystem_paths")
    @classmethod
    def _check_filesystem_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        return validate_allowed_paths(paths)

    @field_validator("procfs_root", "cgroup_root")
    @classmethod
    def _check_absolute(cls, path: Path) -> Path:
        if not path.is_absolute():
            raise ValueError("diagnostic roots must be absolute paths")
        return path

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
