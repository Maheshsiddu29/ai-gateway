"""Streaming responses through the full application with a stubbed upstream.

The in-process ASGI transport buffers the response body, so these tests check
framing and error semantics. Incremental delivery and client disconnects over
real sockets are covered in test_live_server.py.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import httpx
import pytest

from gateway.main import build_app
from gateway.providers.base import TextDelta
from gateway.providers.factory import build_provider
from tests.fakes import (
    AUTH_HEADERS,
    FAKE_UPSTREAM_KEY,
    SSE_HEADERS,
    ControlledStream,
    FakeProvider,
    UpstreamHandler,
    UpstreamStub,
    make_config,
    make_settings,
    openai_chunk,
    serve,
    sse_body,
)

CONFIG: dict[str, object] = {
    "providers": {
        "openai": {
            "type": "openai_compatible",
            "base_url": "https://llm.test/v1",
            "api_key_env": "OPENAI_API_KEY",
        }
    },
    "models": {"fast": {"provider": "openai", "model": "upstream-fast"}},
    # These tests check how single upstream responses are mapped; retries are
    # covered in test_resilience.py.
    "resilience": {"max_attempts_per_provider": 1},
}
STREAM_REQUEST = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True}
USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


@asynccontextmanager
async def gateway(handler: UpstreamHandler) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(
        make_settings(),
        make_config(CONFIG, {"openai": FAKE_UPSTREAM_KEY}),
        partial(build_provider, transport=UpstreamStub(handler)),
    )
    async with serve(app) as client:
        yield client


def frames(body: str) -> list[str]:
    """Split an SSE body into ``data:`` payloads, checking the framing."""
    assert body.endswith("\n\n")
    events = body.removesuffix("\n\n").split("\n\n")
    assert all(event.startswith("data: ") and "\n" not in event for event in events)
    return [event.removeprefix("data: ") for event in events]


def stream_response(*payloads: object, done: bool = True) -> httpx.Response:
    return httpx.Response(200, headers=SSE_HEADERS, content=sse_body(*payloads, done=done))


async def post_stream(
    client: httpx.AsyncClient, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions", json=body or STREAM_REQUEST, headers=AUTH_HEADERS
    )


async def test_streams_openai_compatible_chunks() -> None:
    upstream = stream_response(
        openai_chunk("Hel"), openai_chunk("lo"), openai_chunk(finish_reason="stop")
    )

    async with gateway(lambda _: upstream) as client:
        response = await post_stream(client)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    *chunks, done = frames(response.text)
    assert done == "[DONE]"
    parsed = [json.loads(chunk) for chunk in chunks]
    assert [chunk["choices"][0]["delta"] for chunk in parsed] == [
        {"role": "assistant", "content": ""},
        {"content": "Hel"},
        {"content": "lo"},
        {},
    ]
    assert [chunk["choices"][0]["finish_reason"] for chunk in parsed] == [None, None, None, "stop"]
    assert {chunk["id"] for chunk in parsed} == {parsed[0]["id"]}
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in parsed)
    assert all(chunk["model"] == "fast" for chunk in parsed)


async def test_includes_usage_chunk_only_when_requested() -> None:
    upstream = stream_response(
        openai_chunk("x", finish_reason="stop"), {"choices": [], "usage": USAGE}
    )
    with_usage = {**STREAM_REQUEST, "stream_options": {"include_usage": True}}

    async with gateway(lambda _: upstream) as client:
        requested = frames((await post_stream(client, with_usage)).text)
        default = frames((await post_stream(client)).text)

    usage_chunk = json.loads(requested[-2])
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == USAGE
    assert all("usage" not in json.loads(frame) for frame in default[:-1])


async def test_errors_before_output_use_http_status() -> None:
    async with gateway(lambda _: httpx.Response(429, headers={"retry-after": "2"})) as client:
        response = await post_stream(client)

    assert response.status_code == 429
    assert response.headers["content-type"] == "application/json"
    assert response.headers["retry-after"] == "2"
    assert response.json()["error"]["type"] == "provider_rate_limited"


async def test_unknown_model_is_rejected_before_streaming() -> None:
    async with gateway(lambda _: stream_response()) as client:
        response = await post_stream(client, {**STREAM_REQUEST, "model": "missing"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("failure", "error_type", "code"),
    [
        (None, "provider_unavailable", "provider_stream_incomplete"),
        (httpx.ReadTimeout("stalled"), "provider_timeout", "provider_read_timeout"),
        (
            httpx.RemoteProtocolError("peer closed"),
            "provider_unavailable",
            "provider_connection_error",
        ),
    ],
)
async def test_interruption_after_partial_output(
    failure: Exception | None, error_type: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    upstream_body = ControlledStream(sse_body(openai_chunk("partial"), done=False), failure)
    caplog.set_level(logging.INFO)

    async with gateway(
        lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=upstream_body)
    ) as client:
        response = await post_stream(client)

    assert response.status_code == 200
    payloads = frames(response.text)
    assert "[DONE]" not in payloads
    assert json.loads(payloads[1])["choices"][0]["delta"] == {"content": "partial"}
    error = json.loads(payloads[-1])["error"]
    assert (error["type"], error["code"]) == (error_type, code)
    assert error["request_id"] == response.headers["x-request-id"]
    assert upstream_body.closed.is_set()
    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    assert record.__dict__["outcome"] == "error"
    assert record.__dict__["error_code"] == code


async def test_unexpected_errors_mid_stream_are_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    provider = FakeProvider(events=[TextDelta("partial"), RuntimeError("internal detail")])
    app = build_app(make_settings(), make_config(CONFIG, {"openai": None}), lambda *_: provider)

    async with serve(app) as client:
        response = await post_stream(client)

    payloads = frames(response.text)
    assert json.loads(payloads[-1])["error"]["type"] == "internal_error"
    assert "internal detail" not in response.text
    assert "internal detail" in caplog.text


async def test_successful_stream_releases_the_upstream_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream_body = ControlledStream(sse_body(openai_chunk("x", finish_reason="stop")), None)
    caplog.set_level(logging.INFO)

    async with gateway(
        lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=upstream_body)
    ) as client:
        await post_stream(client)

    assert upstream_body.closed.is_set()
    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    assert record.__dict__["outcome"] == "success"
    assert record.__dict__["stream"] is True
    assert "time_to_first_token_ms" in record.__dict__
