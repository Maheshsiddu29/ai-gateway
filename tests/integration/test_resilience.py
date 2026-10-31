"""Retries and circuit breaking through the full application."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import httpx
import pytest

from gateway.admission.usage import UsageLedger
from gateway.core.config import CircuitBreakerConfig, ResilienceConfig
from gateway.core.metrics import Metrics
from gateway.errors import ProviderUnavailableError
from gateway.main import build_app
from gateway.providers.base import ProviderRequest
from gateway.providers.factory import build_provider
from gateway.resilience.breaker import CircuitBreaker
from gateway.resilience.deadline import Deadline
from gateway.resilience.retry import RetryPolicy
from gateway.schemas.completions import ChatMessage
from gateway.services.execution import Candidate, ProviderExecutor
from tests.fakes import (
    AUTH_HEADERS,
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

FAST_RETRIES: dict[str, Any] = {
    "max_attempts_per_provider": 3,
    "retry_initial_backoff_seconds": 0.01,
    "retry_max_backoff_seconds": 0.02,
    "retry_jitter_ratio": 0,
    "max_retry_after_seconds": 1,
}

BREAKER: dict[str, Any] = {
    "max_attempts_per_provider": 1,
    "circuit_breaker": {"failure_threshold": 2, "recovery_seconds": 0.1},
}

type Outcome = httpx.Response | Exception


def sequence(*outcomes: Outcome) -> UpstreamHandler:
    remaining = list(outcomes)

    def handle(_: httpx.Request) -> httpx.Response:
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handle


def ok(text: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def stream_ok(*texts: str) -> httpx.Response:
    chunks = [openai_chunk(text) for text in texts]
    body = sse_body(*chunks, openai_chunk(finish_reason="stop"))
    return httpx.Response(200, headers=SSE_HEADERS, content=body)


@asynccontextmanager
async def gateway(
    handler: UpstreamHandler, **resilience: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, UpstreamStub]]:
    stub = UpstreamStub(handler)
    config: dict[str, object] = {
        "providers": {"openai": {"type": "openai_compatible", "base_url": "https://llm.test/v1"}},
        "models": {"fast": {"provider": "openai", "model": "m"}},
        "resilience": {**FAST_RETRIES, **resilience},
    }
    app = build_app(
        make_settings(),
        make_config(config, {"openai": None}),
        partial(build_provider, transport=stub),
    )
    async with serve(app) as client:
        yield client, stub


async def post(client: httpx.AsyncClient, *, stream: bool = False) -> httpx.Response:
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    return await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)


def stream_text(response: httpx.Response) -> tuple[str, list[str]]:
    payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    text = "".join(
        json.loads(payload)["choices"][0]["delta"].get("content", "")
        for payload in payloads
        if payload != "[DONE]" and "choices" in json.loads(payload)
    )
    return text, payloads


class TestRetries:
    @pytest.mark.parametrize(
        "failure",
        [
            httpx.Response(503),
            httpx.Response(500),
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.ConnectError("connection refused"),
            httpx.ConnectTimeout("connect timed out"),
        ],
        ids=["503", "500", "429", "connection-refused", "connect-timeout"],
    )
    async def test_transient_failure_then_success(self, failure: Outcome) -> None:
        async with gateway(sequence(failure, ok("recovered"))) as (client, stub):
            response = await post(client)
            metrics = (await client.get("/metrics")).text

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "recovered"
        assert len(stub.requests) == 2
        assert 'gateway_provider_retries_total{provider="openai"} 1.0' in metrics

    async def test_retries_are_bounded(self) -> None:
        async with gateway(lambda _: httpx.Response(503)) as (client, stub):
            response = await post(client)

        assert response.status_code == 502
        assert len(stub.requests) == FAST_RETRIES["max_attempts_per_provider"]

    @pytest.mark.parametrize(
        ("failure", "status"),
        [
            (httpx.ReadTimeout("no response"), 504),
            (httpx.RemoteProtocolError("server disconnected"), 502),
            (httpx.Response(504), 504),
            (httpx.Response(400), 400),
            (httpx.Response(401), 502),
            (httpx.Response(200, content=b"not json"), 502),
        ],
        ids=["read-timeout", "dropped-before-headers", "504", "400", "401", "malformed"],
    )
    async def test_failures_that_may_have_run_or_are_not_transient_are_not_retried(
        self, failure: Outcome, status: int
    ) -> None:
        async with gateway(sequence(failure, ok())) as (client, stub):
            response = await post(client)

        assert response.status_code == status
        assert len(stub.requests) == 1

    async def test_retry_after_beyond_the_cap_is_not_waited_for(self) -> None:
        failure = httpx.Response(429, headers={"retry-after": "30"})

        async with gateway(sequence(failure, ok())) as (client, stub):
            started = time.monotonic()
            response = await post(client)

        assert response.status_code == 429
        assert response.headers["retry-after"] == "30"
        assert len(stub.requests) == 1
        assert time.monotonic() - started < 1

    async def test_retries_stop_at_the_deadline(self) -> None:
        async with gateway(
            lambda _: httpx.Response(503),
            request_deadline_seconds=0.3,
            max_attempts_per_provider=5,
            retry_initial_backoff_seconds=0.2,
            retry_max_backoff_seconds=0.2,
        ) as (client, stub):
            started = time.monotonic()
            response = await post(client)
            elapsed = time.monotonic() - started

        assert response.status_code == 502
        # After the first backoff only 0.1 s remain, too little for another 0.2 s wait.
        assert len(stub.requests) == 2
        assert elapsed < 0.3

    async def test_stream_failure_before_output_is_retried_without_duplicating_content(
        self,
    ) -> None:
        async with gateway(sequence(httpx.Response(503), stream_ok("Hel", "lo"))) as (
            client,
            stub,
        ):
            response = await post(client, stream=True)

        text, payloads = stream_text(response)
        assert text == "Hello"
        assert payloads[-1] == "[DONE]"
        assert len(stub.requests) == 2

    async def test_stream_failure_after_output_is_not_retried(self) -> None:
        body = ControlledStream(
            sse_body(openai_chunk("partial"), done=False),
            httpx.RemoteProtocolError("peer closed"),
        )
        broken = httpx.Response(200, headers=SSE_HEADERS, stream=body)

        async with gateway(sequence(broken, stream_ok("again"))) as (client, stub):
            response = await post(client, stream=True)

        text, payloads = stream_text(response)
        assert text == "partial"
        assert "[DONE]" not in payloads
        assert json.loads(payloads[-1])["error"]["type"] == "provider_unavailable"
        assert len(stub.requests) == 1

    async def test_malformed_stream_event_is_not_retried(self) -> None:
        malformed = httpx.Response(200, headers=SSE_HEADERS, content=b"data: {broken\n\n")

        async with gateway(sequence(malformed, stream_ok("x"))) as (client, stub):
            response = await post(client, stream=True)

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "malformed_provider_response"
        assert len(stub.requests) == 1


class TestCircuitBreaker:
    async def test_opens_after_failures_and_recovers_after_a_successful_probe(self) -> None:
        handler = sequence(httpx.Response(503), httpx.Response(503), ok("probe"), ok("after"))

        async with gateway(handler, **BREAKER) as (client, stub):
            failures = [(await post(client)).status_code for _ in range(2)]
            refused = await post(client)
            await asyncio.sleep(0.15)
            probe = await post(client)
            after = await post(client)
            metrics = (await client.get("/metrics")).text

        assert failures == [502, 502]
        assert refused.status_code == 502
        assert refused.json()["error"]["code"] == "provider_circuit_open"
        assert (probe.status_code, after.status_code) == (200, 200)
        # The refused request never reached the provider.
        assert len(stub.requests) == 4
        for state in ("open", "half_open", "closed"):
            assert f'provider="openai",state="{state}"' in metrics

    async def test_a_failed_probe_reopens_the_circuit(self) -> None:
        handler = sequence(httpx.Response(503), httpx.Response(503), httpx.Response(503))

        async with gateway(handler, **BREAKER) as (client, stub):
            for _ in range(2):
                await post(client)
            await asyncio.sleep(0.15)
            probe = await post(client)
            refused = await post(client)

        assert probe.status_code == 502
        assert refused.json()["error"]["code"] == "provider_circuit_open"
        assert len(stub.requests) == 3

    @pytest.mark.parametrize(
        "failure",
        [httpx.Response(429), httpx.Response(400), httpx.Response(401)],
        ids=["rate-limited", "rejected", "unauthorized"],
    )
    async def test_client_and_rate_limit_errors_do_not_open_the_circuit(
        self, failure: httpx.Response
    ) -> None:
        async with gateway(lambda _: failure, **BREAKER) as (client, stub):
            for _ in range(4):
                await post(client)

        assert len(stub.requests) == 4


async def test_cancellation_during_backoff_stops_further_attempts() -> None:
    provider = FakeProvider(error=ProviderUnavailableError("down", may_have_processed=False))
    sleeping = asyncio.Event()

    async def blocking_sleep(_: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    config = ResilienceConfig(circuit_breaker=CircuitBreakerConfig(enabled=False))
    breaker = CircuitBreaker(
        "fake", config.circuit_breaker, clock=time.monotonic, on_transition=lambda *_: None
    )
    executor = ProviderExecutor(
        {"fake": provider},
        {"fake": breaker},
        RetryPolicy.from_config(config),
        Metrics(),
        sleep=blocking_sleep,
    )
    request = ProviderRequest(
        model="m",
        messages=(ChatMessage(role="user", content="hi"),),
        max_tokens=None,
        temperature=None,
        top_p=None,
        stop=(),
        include_usage=False,
    )
    task = asyncio.create_task(
        executor.complete([Candidate("fake", request)], Deadline(10), UsageLedger())
    )
    await sleeping.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.requests) == 1
