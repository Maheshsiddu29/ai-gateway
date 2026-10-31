"""In-memory test doubles shared across test modules."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from gateway.core.config import Environment, GatewayConfig, LoadedConfig, Settings
from gateway.providers.base import Completion, ProviderRequest, StreamEvent

GATEWAY_API_KEY = "test-gateway-key-0123456789abcdef"
AUTH_HEADERS = {"Authorization": f"Bearer {GATEWAY_API_KEY}"}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_keys": (GATEWAY_API_KEY,),
        "environment": Environment.DEVELOPMENT,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def make_config(raw: dict[str, object], api_keys: dict[str, str | None]) -> LoadedConfig:
    return LoadedConfig(
        gateway=GatewayConfig.model_validate(raw),
        provider_api_keys={
            name: SecretStr(key) if key is not None else None for name, key in api_keys.items()
        },
    )


class FakeProvider:
    def __init__(
        self,
        name: str = "fake",
        *,
        completion: Completion | None = None,
        events: list[StreamEvent] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._name = name
        self._completion = completion or Completion(
            content="hello", finish_reason="stop", usage=None
        )
        self._events = events or []
        self._error = error
        self.requests: list[ProviderRequest] = []
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, request: ProviderRequest) -> Completion:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._completion

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        self.closed = True


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield an in-process client for it."""
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client,
    ):
        yield client
