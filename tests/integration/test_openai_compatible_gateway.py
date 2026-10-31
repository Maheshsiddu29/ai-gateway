"""End-to-end requests through the gateway to a stubbed OpenAI-compatible upstream."""

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
    FAKE_UPSTREAM_KEY,
    GATEWAY_API_KEY,
    UpstreamHandler,
    UpstreamStub,
    make_config,
    make_settings,
    serve,
)

CONFIG: dict[str, object] = {
    "providers": {
        "openai": {
            "type": "openai_compatible",
            "base_url": "https://llm.test/v1",
            "api_key_env": "OPENAI_API_KEY",
        }
    },
    "models": {"fast": {"provider": "openai", "model": "upstream-fast", "default_max_tokens": 100}},
}
REQUEST = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}


def completion_for(text: str) -> dict[str, object]:
    return {
        "id": "upstream-id",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }


@asynccontextmanager
async def gateway(
    handler: UpstreamHandler,
) -> AsyncIterator[tuple[httpx.AsyncClient, UpstreamStub]]:
    stub = UpstreamStub(handler)
    app = build_app(
        make_settings(),
        make_config(CONFIG, {"openai": FAKE_UPSTREAM_KEY}),
        partial(build_provider, transport=stub),
    )
    async with serve(app) as client:
        yield client, stub


async def test_resolves_the_alias_and_returns_it_as_the_model() -> None:
    async with gateway(lambda _: httpx.Response(200, json=completion_for("hello"))) as (
        client,
        stub,
    ):
        response = await client.post("/v1/chat/completions", json=REQUEST, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["model"] == "fast"
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    assert stub.json_bodies()[0]["model"] == "upstream-fast"
    assert stub.json_bodies()[0]["max_tokens"] == 100


async def test_client_headers_are_not_forwarded_upstream() -> None:
    client_headers = {
        **AUTH_HEADERS,
        "X-Api-Key": "attacker-key",
        "OpenAI-Organization": "org-attacker",
        "Anthropic-Version": "1999-01-01",
        "Cookie": "session=abc",
        "X-Forwarded-For": "10.0.0.1",
        "X-Forwarded-Host": "evil.test",
        "Proxy-Authorization": "Basic abc",
    }

    async with gateway(lambda _: httpx.Response(200, json=completion_for("ok"))) as (client, stub):
        await client.post("/v1/chat/completions", json=REQUEST, headers=client_headers)

    upstream_headers = stub.requests[0].headers
    assert upstream_headers["authorization"] == f"Bearer {FAKE_UPSTREAM_KEY}"
    assert upstream_headers["host"] == "llm.test"
    assert set(upstream_headers.keys()) == {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-length",
        "content-type",
        "authorization",
    }
    assert GATEWAY_API_KEY not in str(stub.requests[0].headers)


@pytest.mark.parametrize("status", [401, 400, 429, 500])
async def test_credentials_never_reach_clients_or_logs(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    def echo_credentials(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["authorization"]
        return httpx.Response(
            status,
            headers={"x-echo": authorization},
            json={"error": {"type": "error", "message": f"received {authorization}"}},
        )

    caplog.set_level(logging.DEBUG)
    async with gateway(echo_credentials) as (client, _):
        response = await client.post("/v1/chat/completions", json=REQUEST, headers=AUTH_HEADERS)

    assert response.status_code >= 400
    for secret in (FAKE_UPSTREAM_KEY, GATEWAY_API_KEY):
        assert secret not in response.text
        assert secret not in str(response.headers)
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("upstream", "status", "error_type"),
    [
        (httpx.Response(429, headers={"retry-after": "5"}), 429, "provider_rate_limited"),
        (httpx.Response(503), 502, "provider_unavailable"),
        (httpx.Response(401), 502, "provider_configuration_error"),
        (httpx.Response(200, content=b"<html>"), 502, "provider_unavailable"),
    ],
)
async def test_upstream_failures_map_to_gateway_errors(
    upstream: httpx.Response, status: int, error_type: str
) -> None:
    async with gateway(lambda _: upstream) as (client, _):
        response = await client.post("/v1/chat/completions", json=REQUEST, headers=AUTH_HEADERS)

    assert response.status_code == status
    assert response.json()["error"]["type"] == error_type


async def test_upstream_timeout() -> None:
    def time_out(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with gateway(time_out) as (client, _):
        response = await client.post("/v1/chat/completions", json=REQUEST, headers=AUTH_HEADERS)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "provider_read_timeout"


async def test_concurrent_requests_are_processed_independently() -> None:
    concurrency = 20
    # Every upstream call waits until all requests are in flight, so the test only
    # passes if the gateway handles them concurrently.
    barrier = asyncio.Barrier(concurrency)

    async def answer(request: httpx.Request) -> httpx.Response:
        await barrier.wait()
        prompt = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json=completion_for(f"echo {prompt}"))

    async def send(client: httpx.AsyncClient, index: int) -> str:
        body = {"model": "fast", "messages": [{"role": "user", "content": f"request {index}"}]}
        response = await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)
        content: str = response.json()["choices"][0]["message"]["content"]
        return content

    async with gateway(answer) as (client, _), asyncio.timeout(5):
        results = await asyncio.gather(*(send(client, i) for i in range(concurrency)))

    assert results == [f"echo request {i}" for i in range(concurrency)]
