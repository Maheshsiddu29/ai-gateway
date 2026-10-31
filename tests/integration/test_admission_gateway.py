"""Admission control through the full application, backed by a real Redis server."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from gateway.admission.controller import READINESS_CACHE_SECONDS
from gateway.admission.redis_store import KEY_PREFIX
from gateway.admission.tokens import estimate_input_tokens
from gateway.core.security import StaticKeyAuthenticator
from gateway.main import build_app
from gateway.providers.factory import build_provider
from gateway.schemas.completions import ChatMessage
from tests.fakes import (
    AUTH_HEADERS,
    GATEWAY_API_KEY,
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
from tests.polling import eventually
from tests.redis_server import RedisServer
from tests.servers import run_server

MAX_OUTPUT_TOKENS = 100
MESSAGES = [{"role": "user", "content": "hi"}]
RESERVED_TOKENS = (
    estimate_input_tokens([ChatMessage(role="user", content="hi")]) + MAX_OUTPUT_TOKENS
)
_principal = StaticKeyAuthenticator([SecretStr(GATEWAY_API_KEY)]).authenticate(GATEWAY_API_KEY)
assert _principal is not None
PRINCIPAL_ID = _principal.id
STATE = f"{KEY_PREFIX}:{{{PRINCIPAL_ID}}}"


def completion(usage: dict[str, int] | None = None) -> httpx.Response:
    body: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def gateway_config(
    limits: dict[str, int], *, deadline: float = 60, max_output_tokens: int | None = None
) -> dict[str, object]:
    model: dict[str, object] = {"provider": "openai", "model": "m"}
    model["max_output_tokens"] = max_output_tokens or MAX_OUTPUT_TOKENS
    return {
        "providers": {"openai": {"type": "openai_compatible", "base_url": "https://llm.test/v1"}},
        "models": {"fast": model},
        "resilience": {"request_deadline_seconds": deadline},
        "admission": {"enabled": True, "default_limits": limits},
    }


def build(
    redis_server: RedisServer, limits: dict[str, int], stub: UpstreamStub, **options: Any
) -> Any:
    return build_app(
        make_settings(redis_url=redis_server.url),
        make_config(gateway_config(limits, **options), {"openai": None}),
        partial(build_provider, transport=stub),
    )


@asynccontextmanager
async def gateway(
    redis_server: RedisServer, limits: dict[str, int], handler: UpstreamHandler, **options: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, UpstreamStub]]:
    stub = UpstreamStub(handler)
    async with serve(build(redis_server, limits, stub, **options)) as client:
        yield client, stub


async def post(client: httpx.AsyncClient, **fields: Any) -> httpx.Response:
    body = {"model": "fast", "messages": MESSAGES, **fields}
    return await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)


async def token_balance(redis: Redis) -> float:
    value = await redis.hget(f"{STATE}:tokens", "tokens")
    assert value is not None
    return float(value)


async def reservation_records(redis: Redis) -> list[dict[str, str]]:
    keys = [key async for key in redis.scan_iter(f"{STATE}:reservation:*")]
    records = [await redis.hgetall(key) for key in keys]
    return [{str(field): str(value) for field, value in record.items()} for record in records]


class TestRequestLimit:
    async def test_returns_429_with_retry_after_and_never_calls_the_provider(
        self, redis_server: RedisServer
    ) -> None:
        limits = {"requests_per_minute": 60, "request_burst": 2}

        async with gateway(redis_server, limits, lambda _: completion()) as (client, stub):
            statuses = [(await post(client)).status_code for _ in range(2)]
            rejected = await post(client)

        assert statuses == [200, 200]
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "1"
        error = rejected.json()["error"]
        assert (error["type"], error["code"]) == ("rate_limited", "requests_per_minute_exceeded")
        assert len(stub.requests) == 2

    async def test_requests_rejected_before_admission_consume_no_quota(
        self, redis_server: RedisServer
    ) -> None:
        limits = {"requests_per_minute": 1, "request_burst": 1}

        async with gateway(redis_server, limits, lambda _: completion()) as (client, _):
            unknown_model = await post(client, model="missing")
            over_ceiling = await post(client, max_tokens=MAX_OUTPUT_TOKENS + 1)
            accepted = await post(client)

        assert unknown_model.status_code == 404
        assert over_ceiling.json()["error"]["code"] == "max_tokens_exceeds_limit"
        assert accepted.status_code == 200

    async def test_limits_are_per_key(self, redis_server: RedisServer) -> None:
        other_key = "other-gateway-key-0123456789abcdef"
        limits = {"requests_per_minute": 1, "request_burst": 1}
        stub = UpstreamStub(lambda _: completion())
        app = build_app(
            make_settings(redis_url=redis_server.url, api_keys=(GATEWAY_API_KEY, other_key)),
            make_config(gateway_config(limits), {"openai": None}),
            partial(build_provider, transport=stub),
        )
        body = {"model": "fast", "messages": MESSAGES}

        async with serve(app) as client:
            first = await post(client)
            second = await post(client)
            other = await client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {other_key}"},
            )

        assert (first.status_code, second.status_code, other.status_code) == (200, 429, 200)


class TestTokenLimit:
    async def test_reconciles_the_reservation_with_reported_usage(
        self, redis_server: RedisServer, redis_client: Redis
    ) -> None:
        limits = {"tokens_per_minute": 60, "token_burst": 1000}
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}

        async with gateway(redis_server, limits, lambda _: completion(usage)) as (client, stub):
            response = await post(client)

        assert response.status_code == 200
        # max_tokens is sent so the provider enforces the bound that was reserved.
        assert stub.json_bodies()[0]["max_tokens"] == MAX_OUTPUT_TOKENS
        assert await token_balance(redis_client) == pytest.approx(970, abs=2)
        [record] = await reservation_records(redis_client)
        assert record["state"] == "settled"
        assert record["charged"] == "30"

    async def test_charges_the_full_reservation_when_usage_is_unknown(
        self, redis_server: RedisServer, redis_client: Redis
    ) -> None:
        limits = {"tokens_per_minute": 60, "token_burst": 1000}

        async with gateway(redis_server, limits, lambda _: completion()) as (client, _):
            await post(client)

        assert await token_balance(redis_client) == pytest.approx(1000 - RESERVED_TOKENS, abs=2)

    async def test_refunds_everything_when_the_provider_did_not_run_the_request(
        self, redis_server: RedisServer, redis_client: Redis
    ) -> None:
        limits = {"tokens_per_minute": 60, "token_burst": 1000}

        async with gateway(redis_server, limits, lambda _: httpx.Response(503)) as (client, _):
            response = await post(client)

        assert response.status_code == 502
        assert await token_balance(redis_client) == pytest.approx(1000, abs=2)

    async def test_rejects_with_retry_after_once_tokens_are_exhausted(
        self, redis_server: RedisServer
    ) -> None:
        limits = {"tokens_per_minute": 60, "token_burst": 2 * RESERVED_TOKENS + 10}

        async with gateway(redis_server, limits, lambda _: completion()) as (client, _):
            statuses = [(await post(client)).status_code for _ in range(2)]
            rejected = await post(client)

        assert statuses == [200, 200]
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "tokens_per_minute_exceeded"
        assert int(rejected.headers["retry-after"]) > 0

    async def test_a_reservation_that_can_never_fit_is_a_client_error(
        self, redis_server: RedisServer, redis_client: Redis
    ) -> None:
        limits = {"tokens_per_minute": 60, "token_burst": 50}

        async with gateway(redis_server, limits, lambda _: completion()) as (client, stub):
            response = await post(client)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "token_reservation_exceeds_limit"
        assert stub.requests == []
        assert [key async for key in redis_client.scan_iter(f"{KEY_PREFIX}:*")] == []

    async def test_requires_an_output_bound(self, redis_server: RedisServer) -> None:
        stub = UpstreamStub(lambda _: completion())
        config = gateway_config({"tokens_per_minute": 1000})
        config["models"] = {"fast": {"provider": "openai", "model": "m"}}
        app = build_app(
            make_settings(redis_url=redis_server.url),
            make_config(config, {"openai": None}),
            partial(build_provider, transport=stub),
        )

        async with serve(app) as client:
            missing = await post(client)
            explicit = await post(client, max_tokens=50)

        assert missing.json()["error"]["code"] == "max_tokens_required"
        assert explicit.status_code == 200


class TestConcurrencyLimit:
    async def test_rejects_requests_beyond_the_limit_while_others_run(
        self, redis_server: RedisServer
    ) -> None:
        release = asyncio.Event()
        arrived = asyncio.Event()

        async def slow(_: httpx.Request) -> httpx.Response:
            arrived.set()
            await release.wait()
            return completion()

        async with gateway(redis_server, {"max_concurrent_requests": 1}, slow) as (client, _):
            first = asyncio.create_task(post(client))
            await arrived.wait()
            rejected = await post(client)
            release.set()
            completed = await first
            after = await post(client)

        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "concurrency_limit_exceeded"
        assert "retry-after" not in rejected.headers
        assert (completed.status_code, after.status_code) == (200, 200)

    async def test_deadline_expiry_releases_the_lease(
        self, redis_server: RedisServer, redis_client: Redis
    ) -> None:
        bodies = iter([ControlledStream(), None])

        def first_stalls(_: httpx.Request) -> httpx.Response:
            body = next(bodies)
            return httpx.Response(200, stream=body) if body else completion()

        async with gateway(
            redis_server, {"max_concurrent_requests": 1}, first_stalls, deadline=0.2
        ) as (client, _):
            timed_out = await post(client)
            after = await post(client)

        assert timed_out.status_code == 504
        assert after.status_code == 200
        assert await redis_client.zcard(f"{STATE}:leases") == 0

    async def test_client_disconnect_mid_stream_releases_the_lease_and_charges_it(
        self, redis_server: RedisServer, redis_client: Redis
    ) -> None:
        upstream_body = ControlledStream(sse_body(openai_chunk("Hel"), done=False))
        stub = UpstreamStub(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=upstream_body)
        )
        limits = {"max_concurrent_requests": 1, "tokens_per_minute": 60, "token_burst": 1000}
        body = {"model": "fast", "messages": MESSAGES, "stream": True}

        async with (
            run_server(build(redis_server, limits, stub)) as url,
            httpx.AsyncClient(base_url=url, headers=AUTH_HEADERS) as client,
            asyncio.timeout(5),
        ):
            async with client.stream("POST", "/v1/chat/completions", json=body) as response:
                async for line in response.aiter_lines():
                    if '"Hel"' in line:
                        break
            await upstream_body.closed.wait()

            async def lease_released() -> bool:
                return await redis_client.zcard(f"{STATE}:leases") == 0

            await eventually(lease_released)

        [record] = await reservation_records(redis_client)
        assert record["state"] == "settled"
        # The provider was generating when the client left; usage is unknown.
        assert int(record["charged"]) == RESERVED_TOKENS


class TestMultipleInstances:
    async def test_concurrency_limit_holds_across_instances(
        self, redis_server: RedisServer
    ) -> None:
        release = asyncio.Event()

        async def slow(_: httpx.Request) -> httpx.Response:
            await release.wait()
            return completion()

        stubs = [UpstreamStub(slow), UpstreamStub(slow)]
        limits = {"max_concurrent_requests": 3}

        async with (
            serve(build(redis_server, limits, stubs[0])) as first,
            serve(build(redis_server, limits, stubs[1])) as second,
        ):
            tasks = [
                asyncio.create_task(post(client)) for _ in range(6) for client in (first, second)
            ]
            _, pending = await asyncio.wait(tasks, timeout=2)
            release.set()
            responses = await asyncio.gather(*tasks)

        assert len(pending) == 3
        assert sum(len(stub.requests) for stub in stubs) == 3
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200] * 3 + [429] * 9

    async def test_request_limit_holds_across_instances(self, redis_server: RedisServer) -> None:
        limits = {"requests_per_minute": 1, "request_burst": 5}
        stubs = [UpstreamStub(lambda _: completion()), UpstreamStub(lambda _: completion())]

        async with (
            serve(build(redis_server, limits, stubs[0])) as first,
            serve(build(redis_server, limits, stubs[1])) as second,
        ):
            responses = await asyncio.gather(
                *(post(client) for _ in range(10) for client in (first, second))
            )

        assert sum(response.status_code == 200 for response in responses) == 5
        assert sum(len(stub.requests) for stub in stubs) == 5


class TestRedisFailure:
    async def test_outage_fails_closed_and_readiness_reports_it(
        self, redis_server: RedisServer
    ) -> None:
        async with gateway(redis_server, {"requests_per_minute": 60}, lambda _: completion()) as (
            client,
            stub,
        ):
            assert (await client.get("/health/ready")).json()["admission"] == "enabled"
            await redis_server.stop()

            refused = await post(client)
            # Readiness results are reused for up to READINESS_CACHE_SECONDS.
            await asyncio.sleep(READINESS_CACHE_SECONDS)
            readiness = await client.get("/health/ready")

            await redis_server.start()
            recovered = [(await post(client)).status_code for _ in range(3)]
            await asyncio.sleep(READINESS_CACHE_SECONDS)
            ready_again = await client.get("/health/ready")
            metrics = (await client.get("/metrics")).text

        assert refused.status_code == 503
        assert refused.json()["error"]["type"] == "admission_unavailable"
        assert readiness.status_code == 503
        # Only admitted requests reached the provider; the refused one did not.
        assert len(stub.requests) == 3
        assert recovered == [200, 200, 200]
        assert ready_again.status_code == 200
        assert 'gateway_admission_decisions_total{outcome="unavailable"}' in metrics

    async def test_errors_never_expose_the_redis_url(self, redis_server: RedisServer) -> None:
        async with gateway(redis_server, {"requests_per_minute": 60}, lambda _: completion()) as (
            client,
            _,
        ):
            await redis_server.stop()
            response = await post(client)
            await redis_server.start()

        assert str(redis_server.socket_path) not in response.text
        assert json.loads(response.text)["error"]["message"] == (
            "Request admission is temporarily unavailable."
        )
