"""In-memory test doubles shared across test modules."""

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

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


type UpstreamHandler = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]


class UpstreamStub(httpx.AsyncBaseTransport):
    """An httpx transport that records requests and answers with a handler."""

    def __init__(self, handler: UpstreamHandler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        response = self._handler(request)
        if inspect.isawaitable(response):
            return await response
        return response

    def json_bodies(self) -> list[Any]:
        return [json.loads(request.content) for request in self.requests]


class ControlledStream(httpx.AsyncByteStream):
    """A response body whose chunks are released by the test.

    Push bytes to send a chunk, an exception to fail the read, or None to end the
    body. ``closed`` is set when the client releases the response.
    """

    def __init__(self, *items: bytes | BaseException | None) -> None:
        self._queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        self.closed = asyncio.Event()
        for item in items:
            self.push(item)

    def push(self, item: bytes | BaseException | None) -> None:
        self._queue.put_nowait(item)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (item := await self._queue.get()) is not None:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        self.closed.set()


def sse_body(*payloads: object, done: bool = True) -> bytes:
    """Encode OpenAI-style ``data:`` events, optionally followed by ``[DONE]``."""
    events = [f"data: {json.dumps(payload)}\n\n" for payload in payloads]
    if done:
        events.append("data: [DONE]\n\n")
    return "".join(events).encode()


def sse_events(*events: tuple[str, object]) -> bytes:
    """Encode named events, as used by the Anthropic streaming API."""
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()


SSE_HEADERS = {"content-type": "text/event-stream"}
