"""Agent runtime configuration, read from ``AGENT_*`` environment variables.

The runtime has no configuration file. Everything is validated at startup, so a
misconfigured runtime fails to start instead of failing on its first
investigation. Credentials are held as ``SecretStr`` and never appear in ``repr``
or validation errors.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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

    _split_api_keys = field_validator("api_keys", mode="before")(_split_list)

    @model_validator(mode="after")
    def _check_production_keys(self) -> "Settings":
        if self.environment is Environment.PRODUCTION and any(
            len(key.get_secret_value()) < MIN_PRODUCTION_KEY_LENGTH for key in self.api_keys
        ):
            raise ValueError(
                f"agent API keys must be at least {MIN_PRODUCTION_KEY_LENGTH} characters"
                " in production"
            )
        return self
