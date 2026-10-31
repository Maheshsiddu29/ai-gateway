"""Streaming and cancellation over real sockets.

The gateway and a fake upstream both run in uvicorn on ephemeral localhost ports,
so these tests exercise httpx's connection pool, uvicorn's disconnect signalling,
and incremental delivery without buffering.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive

from gateway.main import build_app
from tests.fakes import AUTH_HEADERS, make_config, make_settings, openai_chunk, sse_body

TIMEOUT_SECONDS = 5
STREAM_REQUEST = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True}


class _DisconnectAwareResponse(StreamingResponse):
    """Reports when the client (the gateway) disconnects.

    A body generator cannot detect this itself: if it is suspended at a ``yield``
    while the response is blocked writing, it is never resumed.
    """

    def __init__(
        self, content: AsyncIterator[bytes], media_type: str, on_disconnect: asyncio.Event
    ) -> None:
        super().__init__(content, media_type=media_type)
        self._on_disconnect = on_disconnect

    async def listen_for_disconnect(self, receive: Receive) -> None:
        await super().listen_for_disconnect(receive)
        self._on_disconnect.set()


class FakeUpstream:
    """An OpenAI-compatible upstream whose progress is controlled by the test."""

    def __init__(self, *, fail_after_first_chunk: bool = False, endless: bool = False) -> None:
        self.fail_after_first_chunk = fail_after_first_chunk
        self.endless = endless
        self.request_received = asyncio.Event()
        self.release = asyncio.Event()
        self.connection_closed = asyncio.Event()

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/chat/completions", self._handle, methods=["POST"])])

    async def _handle(self, request: Request) -> StreamingResponse:
        stream = (await request.json())["stream"]
        self.request_received.set()
        return _DisconnectAwareResponse(
            self._stream_body() if stream else self._json_body(),
            media_type="text/event-stream" if stream else "application/json",
            on_disconnect=self.connection_closed,
        )

    async def _stream_body(self) -> AsyncIterator[bytes]:
        yield sse_body(openai_chunk("Hel"), done=False)
        # Produce output for as long as the gateway keeps reading. uvicorn's send
        # returns without suspending once the peer has gone, so yield to the loop
        # explicitly to let the disconnect be noticed.
        while self.endless:
            yield sse_body(openai_chunk("x" * 65_536), done=False)
            await asyncio.sleep(0)
        await self.release.wait()
        if self.fail_after_first_chunk:
            raise RuntimeError("simulated upstream crash")
        yield sse_body(openai_chunk("lo", finish_reason="stop"))

    async def _json_body(self) -> AsyncIterator[bytes]:
        await self.release.wait()
        body = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
        yield json.dumps(body).encode()


@asynccontextmanager
async def run_server(app: ASGIApp) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(TIMEOUT_SECONDS):
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def running(
    upstream: FakeUpstream, *, deadline_seconds: float = 60
) -> AsyncIterator[httpx.AsyncClient]:
    async with run_server(upstream.app()) as upstream_url:
        config = make_config(
            {
                "providers": {
                    "upstream": {"type": "openai_compatible", "base_url": f"{upstream_url}/v1"}
                },
                "models": {"fast": {"provider": "upstream", "model": "m"}},
                "resilience": {"request_deadline_seconds": deadline_seconds},
            },
            {"upstream": None},
        )
        async with (
            run_server(build_app(make_settings(), config)) as gateway_url,
            httpx.AsyncClient(base_url=gateway_url, headers=AUTH_HEADERS) as client,
        ):
            yield client


class CompletionOutcomes(logging.Handler):
    """Collects ``completion finished`` outcomes and signals when one arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: list[str] = []
        self.received = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "completion finished":
            self.outcomes.append(record.__dict__["outcome"])
            self.received.set()


@pytest.fixture
def completions_log() -> Iterator[CompletionOutcomes]:
    handler = CompletionOutcomes()
    logger = logging.getLogger("gateway.services.completion")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


async def test_chunks_are_delivered_before_the_upstream_finishes() -> None:
    upstream = FakeUpstream()

    async with (
        running(upstream) as client,
        asyncio.timeout(TIMEOUT_SECONDS),
        client.stream("POST", "/v1/chat/completions", json=STREAM_REQUEST) as response,
    ):
        lines = response.aiter_lines()
        received: list[str] = []
        # The upstream is blocked after its first chunk, so this only completes
        # if the gateway forwards chunks as they arrive.
        while not any('"Hel"' in line for line in received):
            received.append(await anext(lines))
        assert not upstream.release.is_set()

        upstream.release.set()
        rest = [line async for line in lines]

    assert "data: [DONE]" in rest


async def test_client_disconnect_during_stream_closes_the_upstream_request(
    completions_log: CompletionOutcomes,
) -> None:
    upstream = FakeUpstream()

    async with running(upstream) as client, asyncio.timeout(TIMEOUT_SECONDS):
        async with client.stream("POST", "/v1/chat/completions", json=STREAM_REQUEST) as response:
            async for line in response.aiter_lines():
                if '"Hel"' in line:
                    break
        # Leaving the block closes the client connection mid-stream.
        await upstream.connection_closed.wait()
        await completions_log.received.wait()

    assert completions_log.outcomes == ["cancelled"]
    assert not upstream.release.is_set()


async def test_client_disconnect_during_non_streaming_request_cancels_upstream_call(
    caplog: pytest.LogCaptureFixture, completions_log: CompletionOutcomes
) -> None:
    upstream = FakeUpstream()
    caplog.set_level(logging.INFO)
    body = {**STREAM_REQUEST, "stream": False}

    async with running(upstream) as client, asyncio.timeout(TIMEOUT_SECONDS):
        request = asyncio.create_task(client.post("/v1/chat/completions", json=body))
        await upstream.request_received.wait()
        request.cancel()

        await upstream.connection_closed.wait()
        await completions_log.received.wait()

    assert completions_log.outcomes == ["cancelled"]
    access = [r for r in caplog.records if r.getMessage() == "request completed"]
    assert access[-1].__dict__["status_code"] == 499


async def test_upstream_crash_mid_stream_is_reported_to_the_client() -> None:
    upstream = FakeUpstream(fail_after_first_chunk=True)

    async with (
        running(upstream) as client,
        asyncio.timeout(TIMEOUT_SECONDS),
        client.stream("POST", "/v1/chat/completions", json=STREAM_REQUEST) as response,
    ):
        upstream.release.set()
        lines = [line async for line in response.aiter_lines() if line]

    assert response.status_code == 200
    assert "data: [DONE]" not in lines
    error = json.loads(lines[-1].removeprefix("data: "))["error"]
    assert error["type"] == "provider_unavailable"


async def test_deadline_closes_a_stream_whose_client_stopped_reading() -> None:
    upstream = FakeUpstream(endless=True)

    async with (
        running(upstream, deadline_seconds=0.5) as client,
        asyncio.timeout(TIMEOUT_SECONDS),
        client.stream("POST", "/v1/chat/completions", json=STREAM_REQUEST) as response,
    ):
        lines = response.aiter_lines()
        await anext(lines)
        # Stop reading. Once socket buffers fill, the gateway's writes block and
        # only the response-level deadline can end the request.
        await upstream.connection_closed.wait()
