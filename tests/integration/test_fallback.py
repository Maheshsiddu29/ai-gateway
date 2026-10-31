"""Configured provider fallback through the full application."""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import httpx
import pytest

from gateway.main import build_app
from gateway.providers.factory import build_provider
from tests.fakes import (
    AUTH_HEADERS,
    SSE_HEADERS,
    ControlledStream,
    UpstreamStub,
    make_config,
    make_settings,
    openai_chunk,
    serve,
    sse_body,
)

PRIMARY_KEY = "sk-primary-secret"
SECONDARY_KEY = "sk-secondary-secret"
ANTHROPIC_KEY = "sk-ant-third-secret"

type HostHandler = Callable[[httpx.Request], httpx.Response]


def config(fallbacks: list[dict[str, str]], **resilience: Any) -> dict[str, object]:
    return {
        "providers": {
            "primary": {
                "type": "openai_compatible",
                "base_url": "https://primary.test/v1",
                "api_key_env": "PRIMARY_KEY",
            },
            "secondary": {
                "type": "openai_compatible",
                "base_url": "https://secondary.test/v1",
                "api_key_env": "SECONDARY_KEY",
            },
            "anthropic": {
                "type": "anthropic",
                "base_url": "https://anthropic.test",
                "api_key_env": "ANTHROPIC_KEY",
            },
        },
        "models": {
            "fast": {
                "provider": "primary",
                "model": "primary-model",
                "max_output_tokens": 50,
                "fallbacks": fallbacks,
            }
        },
        "resilience": {
            "max_attempts_per_provider": 1,
            "circuit_breaker": {"failure_threshold": 1, "recovery_seconds": 60},
            **resilience,
        },
    }


SECONDARY = [{"provider": "secondary", "model": "secondary-model"}]


def ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def stream_ok(text: str) -> httpx.Response:
    body = sse_body(openai_chunk(text), openai_chunk(finish_reason="stop"))
    return httpx.Response(200, headers=SSE_HEADERS, content=body)


@asynccontextmanager
async def gateway(
    hosts: dict[str, HostHandler], fallbacks: list[dict[str, str]] = SECONDARY, **resilience: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, UpstreamStub]]:
    def route(request: httpx.Request) -> httpx.Response:
        return hosts[request.url.host](request)

    stub = UpstreamStub(route)
    app = build_app(
        make_settings(),
        make_config(
            config(fallbacks, **resilience),
            {"primary": PRIMARY_KEY, "secondary": SECONDARY_KEY, "anthropic": ANTHROPIC_KEY},
        ),
        partial(build_provider, transport=stub),
    )
    async with serve(app) as client:
        yield client, stub


def requests_to(stub: UpstreamStub, host: str) -> list[httpx.Request]:
    return [request for request in stub.requests if request.url.host == host]


async def post(client: httpx.AsyncClient, **fields: Any) -> httpx.Response:
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], **fields}
    return await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)


def fail_with(outcome: httpx.Response | Exception) -> HostHandler:
    def handle(_: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handle


class TestNonStreaming:
    @pytest.mark.parametrize(
        "failure",
        [
            httpx.Response(429),
            httpx.Response(403),
            httpx.Response(401),
            httpx.ConnectError("connection refused"),
        ],
        ids=["429", "403", "401", "connection-refused"],
    )
    async def test_falls_back_when_the_primary_did_not_run_the_request(
        self, failure: httpx.Response | Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        hosts = {"primary.test": fail_with(failure), "secondary.test": lambda _: ok("from b")}
        caplog.set_level(logging.INFO)

        async with gateway(hosts) as (client, stub):
            response = await post(client)
            metrics = (await client.get("/metrics")).text

        assert response.status_code == 200
        assert response.json()["model"] == "fast"
        assert response.json()["choices"][0]["message"]["content"] == "from b"
        [sent] = requests_to(stub, "secondary.test")
        assert json.loads(sent.content)["model"] == "secondary-model"
        assert json.loads(sent.content)["max_tokens"] == 50
        assert (
            'gateway_provider_fallbacks_total{from_provider="primary",to_provider="secondary"}'
            in metrics
        )
        [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
        assert record.__dict__["provider"] == "secondary"
        assert record.__dict__["attempts"] == 2

    async def test_each_provider_receives_only_its_own_credential(self) -> None:
        hosts = {
            "primary.test": fail_with(httpx.Response(429)),
            "secondary.test": lambda _: ok("b"),
        }

        async with gateway(hosts) as (_client, stub):
            await post(_client)

        [primary] = requests_to(stub, "primary.test")
        [secondary] = requests_to(stub, "secondary.test")
        assert primary.headers["authorization"] == f"Bearer {PRIMARY_KEY}"
        assert secondary.headers["authorization"] == f"Bearer {SECONDARY_KEY}"
        assert PRIMARY_KEY not in secondary.content.decode() + str(secondary.headers)

    @pytest.mark.parametrize(
        ("failure", "status"),
        [
            (httpx.ReadTimeout("no response"), 504),
            (httpx.RemoteProtocolError("dropped after sending"), 502),
            (httpx.Response(200, content=b"<html>"), 502),
            (httpx.Response(400), 400),
            (httpx.Response(503), 502),
            (httpx.Response(500), 502),
            (httpx.Response(502), 502),
        ],
        ids=["read-timeout", "dropped", "malformed", "400", "503", "500", "502"],
    )
    async def test_does_not_fall_back_when_the_primary_may_have_run_or_the_request_is_bad(
        self, failure: httpx.Response | Exception, status: int
    ) -> None:
        hosts = {"primary.test": fail_with(failure), "secondary.test": lambda _: ok("b")}

        async with gateway(hosts) as (client, stub):
            response = await post(client)

        assert response.status_code == status
        assert requests_to(stub, "secondary.test") == []

    async def test_skips_a_provider_whose_circuit_is_open(self) -> None:
        primary_failures = iter([httpx.Response(503)])
        hosts = {
            "primary.test": lambda _: next(primary_failures),
            "secondary.test": lambda _: ok("b"),
        }

        async with gateway(hosts) as (client, stub):
            first = await post(client)
            second = await post(client)

        assert (first.status_code, second.status_code) == (502, 200)
        # The breaker opened after the first failure, so the second request went
        # straight to the fallback.
        assert len(requests_to(stub, "primary.test")) == 1
        assert len(requests_to(stub, "secondary.test")) == 1

    async def test_returns_the_last_error_when_every_provider_fails(self) -> None:
        hosts = {
            "primary.test": fail_with(httpx.Response(429)),
            "secondary.test": fail_with(httpx.Response(429, headers={"retry-after": "7"})),
        }

        async with gateway(hosts) as (client, _):
            response = await post(client)

        assert response.status_code == 429
        assert response.headers["retry-after"] == "7"

    async def test_an_incompatible_fallback_is_skipped_rather_than_altered(self) -> None:
        fallbacks = [
            {"provider": "anthropic", "model": "claude-model"},
            {"provider": "secondary", "model": "secondary-model"},
        ]
        hosts = {
            "primary.test": fail_with(httpx.Response(429)),
            "secondary.test": lambda _: ok("b"),
        }

        async with gateway(hosts, fallbacks) as (client, stub):
            # The Anthropic adapter rejects temperature on its default API version.
            response = await post(client, temperature=0.3)

        assert response.status_code == 200
        assert requests_to(stub, "anthropic.test") == []
        [sent] = requests_to(stub, "secondary.test")
        assert json.loads(sent.content)["temperature"] == 0.3

    async def test_fallback_attempts_share_the_request_deadline(self) -> None:
        stalled = ControlledStream()
        hosts = {
            "primary.test": lambda _: httpx.Response(200, stream=stalled),
            "secondary.test": lambda _: ok("b"),
        }

        async with gateway(hosts, request_deadline_seconds=0.2) as (client, stub):
            response = await post(client)

        assert response.status_code == 504
        assert response.json()["error"]["type"] == "deadline_exceeded"
        assert requests_to(stub, "secondary.test") == []


class TestStreaming:
    async def test_falls_back_before_the_first_event(self) -> None:
        hosts = {
            "primary.test": fail_with(httpx.Response(429)),
            "secondary.test": lambda _: stream_ok("from b"),
        }

        async with gateway(hosts) as (client, _):
            response = await post(client, stream=True)

        payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1] == "[DONE]"
        contents = [
            json.loads(payload)["choices"][0]["delta"].get("content") for payload in payloads[:-1]
        ]
        assert "from b" in contents

    async def test_failure_after_partial_output_ends_the_stream_without_fallback(self) -> None:
        body = ControlledStream(
            sse_body(openai_chunk("partial"), done=False),
            httpx.RemoteProtocolError("peer closed"),
        )
        hosts = {
            "primary.test": lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body),
            "secondary.test": lambda _: stream_ok("from b"),
        }

        async with gateway(hosts) as (client, stub):
            response = await post(client, stream=True)

        payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert "[DONE]" not in payloads
        assert json.loads(payloads[-1])["error"]["type"] == "provider_unavailable"
        assert "from b" not in response.text
        assert requests_to(stub, "secondary.test") == []
