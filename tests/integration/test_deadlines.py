"""End-to-end request deadlines through the full application."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import httpx
import pytest

from gateway.main import build_app
from gateway.providers.factory import build_provider
from tests.fakes import (
    AUTH_HEADERS,
    METRICS_HEADERS,
    SSE_HEADERS,
    ControlledStream,
    UpstreamHandler,
    UpstreamStub,
    make_config,
    make_settings,
    openai_chunk,
    serve,
    sse_body,
)

DEADLINE_SECONDS = 0.2


def config(deadline: float = DEADLINE_SECONDS) -> dict[str, object]:
    return {
        "providers": {"openai": {"type": "openai_compatible", "base_url": "https://llm.test/v1"}},
        "models": {"fast": {"provider": "openai", "model": "m"}},
        "resilience": {"request_deadline_seconds": deadline},
    }


@asynccontextmanager
async def gateway(handler: UpstreamHandler) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(
        make_settings(),
        make_config(config(), {"openai": None}),
        partial(build_provider, transport=UpstreamStub(handler)),
    )
    async with serve(app) as client:
        yield client


async def post(client: httpx.AsyncClient, *, stream: bool) -> httpx.Response:
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    return await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)


def stalled_response(*chunks: bytes) -> tuple[ControlledStream, UpstreamHandler]:
    # The body never ends: the upstream accepted the request and then went silent.
    body = ControlledStream(*chunks)
    return body, lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body)


async def test_stalled_non_streaming_upstream_returns_504(caplog: pytest.LogCaptureFixture) -> None:
    body = ControlledStream()
    caplog.set_level(logging.INFO)

    async with gateway(lambda _: httpx.Response(200, stream=body)) as client:
        response = await post(client, stream=False)
        metrics = (await client.get("/metrics", headers=METRICS_HEADERS)).text

    assert response.status_code == 504
    assert response.json()["error"]["type"] == "deadline_exceeded"
    assert body.closed.is_set()
    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    assert record.__dict__["error_type"] == "deadline_exceeded"
    assert 'gateway_deadline_exceeded_total{stream="false"} 1.0' in metrics


async def test_stream_stalled_before_output_returns_504() -> None:
    body, handler = stalled_response()

    async with gateway(handler) as client:
        response = await post(client, stream=True)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "request_deadline_exceeded"
    assert body.closed.is_set()


async def test_stream_stalled_after_output_ends_with_an_error_event() -> None:
    body, handler = stalled_response(sse_body(openai_chunk("partial"), done=False))

    async with gateway(handler) as client:
        response = await post(client, stream=True)

    assert response.status_code == 200
    payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert "[DONE]" not in payloads
    assert json.loads(payloads[1])["choices"][0]["delta"] == {"content": "partial"}
    assert json.loads(payloads[-1])["error"]["type"] == "deadline_exceeded"
    assert body.closed.is_set()


async def test_requests_within_the_deadline_are_unaffected() -> None:
    upstream = httpx.Response(
        200, headers=SSE_HEADERS, content=sse_body(openai_chunk("ok", finish_reason="stop"))
    )

    async with gateway(lambda _: upstream) as client:
        response = await post(client, stream=True)

    assert response.text.rstrip().endswith("data: [DONE]")


async def test_stalled_upload_is_cut_off_before_provider_dispatch() -> None:
    received = asyncio.Event()
    provider_called = False

    async def upload() -> AsyncIterator[bytes]:
        yield b'{"model":"fast",'
        received.set()
        await asyncio.Event().wait()

    def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("a stalled upload must not reach the provider")

    async with gateway(upstream) as client, asyncio.timeout(2):
        response = await client.post(
            "/v1/chat/completions",
            content=upload(),
            headers={**AUTH_HEADERS, "content-type": "application/json"},
        )

    assert received.is_set()
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "deadline_exceeded"
    assert not provider_called
