"""End-to-end requests through the gateway to a stubbed Anthropic upstream."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import httpx

from gateway.main import build_app
from gateway.providers.factory import build_provider
from tests.fakes import (
    AUTH_HEADERS,
    FAKE_UPSTREAM_KEY,
    SSE_HEADERS,
    UpstreamHandler,
    UpstreamStub,
    make_config,
    make_settings,
    serve,
    sse_events,
)

CONFIG: dict[str, object] = {
    "providers": {
        "anthropic": {
            "type": "anthropic",
            "base_url": "https://anthropic.test",
            "api_key_env": "ANTHROPIC_API_KEY",
        }
    },
    "models": {
        "balanced": {"provider": "anthropic", "model": "upstream-model", "default_max_tokens": 512}
    },
}
MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]


@asynccontextmanager
async def gateway(
    handler: UpstreamHandler,
) -> AsyncIterator[tuple[httpx.AsyncClient, UpstreamStub]]:
    stub = UpstreamStub(handler)
    app = build_app(
        make_settings(),
        make_config(CONFIG, {"anthropic": FAKE_UPSTREAM_KEY}),
        partial(build_provider, transport=stub),
    )
    async with serve(app) as client:
        yield client, stub


def data_payloads(body: str) -> list[str]:
    return [line.removeprefix("data: ") for line in body.split("\n") if line.startswith("data: ")]


async def test_non_streaming_completion_is_returned_in_openai_format() -> None:
    message = {
        "id": "msg_1",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 9, "output_tokens": 1},
    }

    async with gateway(lambda _: httpx.Response(200, json=message)) as (client, stub):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "balanced", "messages": MESSAGES},
            headers=AUTH_HEADERS,
        )

    body = response.json()
    assert body["choices"][0] == {
        "index": 0,
        "message": {"role": "assistant", "content": "hello"},
        "finish_reason": "stop",
    }
    assert body["usage"] == {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}
    sent = stub.json_bodies()[0]
    assert sent["system"] == [{"type": "text", "text": "be brief"}]
    assert sent["max_tokens"] == 512


async def test_streaming_completion_with_usage() -> None:
    events = sse_events(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    request = {
        "model": "balanced",
        "messages": MESSAGES,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    async with gateway(lambda _: httpx.Response(200, headers=SSE_HEADERS, content=events)) as (
        client,
        _,
    ):
        response = await client.post("/v1/chat/completions", json=request, headers=AUTH_HEADERS)

    *chunks, done = data_payloads(response.text)
    assert done == "[DONE]"
    parsed = [json.loads(chunk) for chunk in chunks]
    assert parsed[1]["choices"][0]["delta"] == {"content": "hello"}
    assert parsed[2]["choices"][0]["finish_reason"] == "stop"
    assert parsed[3]["usage"] == {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}


async def test_overloaded_error_before_output_returns_an_http_error() -> None:
    events = sse_events(
        ("message_start", {"type": "message_start", "message": {"usage": {}}}),
        ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
    )
    request = {"model": "balanced", "messages": MESSAGES, "stream": True}

    async with gateway(lambda _: httpx.Response(200, headers=SSE_HEADERS, content=events)) as (
        client,
        _,
    ):
        response = await client.post("/v1/chat/completions", json=request, headers=AUTH_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "provider_unavailable"


async def test_sampling_parameters_are_rejected_for_the_current_api_version() -> None:
    async with gateway(lambda _: httpx.Response(500)) as (client, stub):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "balanced", "messages": MESSAGES, "temperature": 0.2},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "unsupported_capability"
    assert response.json()["error"]["param"] == "temperature"
    assert stub.requests == []
