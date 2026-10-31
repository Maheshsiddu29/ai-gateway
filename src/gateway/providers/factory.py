from collections.abc import Callable

import httpx
from pydantic import SecretStr

from gateway.core.config import (
    AnthropicProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
)
from gateway.errors import ConfigurationError
from gateway.providers.base import Provider
from gateway.providers.openai_compatible import OpenAICompatibleProvider

type ProviderFactory = Callable[[str, ProviderConfig, SecretStr | None], Provider]


def build_provider(
    name: str,
    config: ProviderConfig,
    api_key: SecretStr | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Provider:
    match config:
        case OpenAICompatibleProviderConfig():
            return OpenAICompatibleProvider.from_config(name, config, api_key, transport)
        case AnthropicProviderConfig():
            raise ConfigurationError("the anthropic provider type is not implemented yet")
