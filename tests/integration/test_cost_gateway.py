"""Gateway accounting with real PostgreSQL, real Redis, and a deterministic provider."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from functools import partial

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text

from gateway.admission.redis_store import KEY_PREFIX
from gateway.core.security import StaticKeyAuthenticator
from gateway.main import build_app
from gateway.providers.base import Completion
from gateway.providers.factory import build_provider
from tests.fakes import (
    SSE_HEADERS,
    ControlledStream,
    FakeProvider,
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


def _key_id(key: str) -> str:
    principal = StaticKeyAuthenticator([SecretStr(key)]).authenticate(key)
    assert principal is not None
    return principal.id


@pytest.fixture
async def gateway_cost(
    redis_server: RedisServer,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeProvider, str, str, str]]:
    database_url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    first_key = f"gateway-cost-test-{uuid.uuid4()}"
    second_key = f"gateway-cost-test-{uuid.uuid4()}"
    first_id = _key_id(first_key)
    second_id = _key_id(second_key)
    team_id = f"team-{uuid.uuid4()}"
    raw: dict[str, object] = {
        "providers": {"fake": {"type": "openai_compatible", "base_url": "https://fake.test/v1"}},
        "models": {"fast": {"provider": "fake", "model": "mock", "max_output_tokens": 100}},
        "admission": {
            "enabled": True,
            "default_limits": {
                "max_concurrent_requests": 10,
                "tokens_per_minute": 1000,
                "token_burst": 1000,
            },
        },
        "cost": {
            "enabled": True,
            "pricing": [
                {
                    "provider": "fake",
                    "model": "mock",
                    "version": "test-v1",
                    "effective_at": "2020-01-01T00:00:00Z",
                    "input_per_million_usd": "0",
                    "output_per_million_usd": "1",
                }
            ],
            "default_key_limits": {"daily_micro_usd": 100, "monthly_micro_usd": 100},
            "principal_teams": {first_id: team_id, second_id: team_id},
            "team_limits": {team_id: {"daily_micro_usd": 100, "monthly_micro_usd": 100}},
            "team_readers": {team_id: [first_id]},
        },
    }
    provider = FakeProvider(completion=Completion(content="ok", finish_reason="stop", usage=None))
    app = build_app(
        make_settings(
            redis_url=redis_server.url, database_url=database_url, api_keys=(first_key, second_key)
        ),
        make_config(raw, {"fake": None}),
        lambda *_: provider,
    )
    async with serve(app) as client:
        yield client, provider, first_key, second_key, team_id


async def test_budget_denial_blocks_upstream_and_analytics_are_scoped(
    gateway_cost: tuple[httpx.AsyncClient, FakeProvider, str, str, str],
    redis_server: RedisServer,
) -> None:
    client, provider, first_key, second_key, team_id = gateway_cost
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}
    first_headers = {"Authorization": f"Bearer {first_key}"}
    second_headers = {"Authorization": f"Bearer {second_key}"}
    first = await client.post("/v1/chat/completions", json=body, headers=first_headers)
    denied = await client.post("/v1/chat/completions", json=body, headers=second_headers)
    assert first.status_code == 200
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "team_daily_budget_exceeded"
    assert len(provider.requests) == 1

    own = await client.get("/v1/usage", headers=first_headers)
    other = await client.get("/v1/usage", headers=second_headers)
    summary = await client.get("/v1/usage/summary", headers=first_headers)
    budgets = await client.get("/v1/budgets", headers=first_headers)
    team = await client.get(f"/v1/budgets?team_id={team_id}", headers=first_headers)
    forbidden = await client.get(f"/v1/budgets?team_id={team_id}", headers=second_headers)
    assert own.status_code == 200
    assert len(own.json()["items"]) == 1
    assert own.json()["items"][0]["state"] == "unresolved"
    assert other.json()["items"] == []
    assert summary.json()["by_provider_model"][0]["unresolved_micro_usd"] == 100
    assert budgets.json()["items"][0]["spent_micro_usd"] == 100
    assert team.json()["items"][0]["spent_micro_usd"] == 100
    assert forbidden.status_code == 403
    redis = Redis.from_url(redis_server.url, decode_responses=True)
    try:
        prefix = f"{KEY_PREFIX}:{{{_key_id(second_key)}}}:reservation:*"
        records = [await redis.hgetall(key) async for key in redis.scan_iter(prefix)]
        assert len(records) == 1
        assert records[0]["charged"] == "0"
    finally:
        await redis.aclose(close_connection_pool=True)


async def test_analytics_range_is_bounded(
    gateway_cost: tuple[httpx.AsyncClient, FakeProvider, str, str, str],
) -> None:
    client, _, first_key, _, _ = gateway_cost
    headers = {"Authorization": f"Bearer {first_key}"}
    response = await client.get(
        "/v1/usage?start=2020-01-01T00:00:00Z&end=2020-03-01T00:00:00Z",
        headers=headers,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("uncertain", [False, True])
async def test_fallback_accounts_each_attempt(
    redis_server: RedisServer,
    uncertain: bool,
) -> None:
    from gateway.errors import ProviderRateLimitError, ProviderUnavailableError
    from gateway.providers.base import Usage

    database_url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    key = f"gateway-cost-test-{uuid.uuid4()}"
    error = (
        ProviderUnavailableError("upstream failed", may_have_processed=True)
        if uncertain
        else ProviderRateLimitError("rejected", may_have_processed=False)
    )
    primary = FakeProvider(name="primary", error=error)
    fallback = FakeProvider(
        name="fallback",
        completion=Completion(content="ok", finish_reason="stop", usage=Usage(1, 1, 2)),
    )
    raw: dict[str, object] = {
        "providers": {
            "primary": {"type": "openai_compatible", "base_url": "https://fake.test/v1"},
            "fallback": {"type": "openai_compatible", "base_url": "https://fake.test/v1"},
        },
        "models": {
            "fast": {
                "provider": "primary",
                "model": "mock",
                "max_output_tokens": 100,
                "fallbacks": [{"provider": "fallback", "model": "mock"}],
            }
        },
        "resilience": {"max_attempts_per_provider": 1},
        "admission": {"enabled": True, "default_limits": {"max_concurrent_requests": 10}},
        "cost": {
            "enabled": True,
            "default_key_limits": {"daily_micro_usd": 200},
            "pricing": [
                {
                    "provider": name,
                    "model": "mock",
                    "version": "test-v1",
                    "effective_at": "2020-01-01T00:00:00Z",
                    "input_per_million_usd": "0",
                    "output_per_million_usd": "1",
                }
                for name in ("primary", "fallback")
            ],
        },
    }
    providers = {"primary": primary, "fallback": fallback}
    app = build_app(
        make_settings(redis_url=redis_server.url, database_url=database_url, api_keys=(key,)),
        make_config(raw, {"primary": None, "fallback": None}),
        lambda name, *_: providers[name],
    )
    headers = {"Authorization": f"Bearer {key}"}
    async with serve(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
        usage = await client.get("/v1/usage", headers=headers)
    assert response.status_code == (502 if uncertain else 200)
    records = usage.json()["items"]
    if uncertain:
        assert len(fallback.requests) == 0
        assert [(row["provider"], row["state"]) for row in records] == [("primary", "unresolved")]
    else:
        assert len(fallback.requests) == 1
        assert {(row["provider"], row["state"]) for row in records} == {
            ("primary", "released"),
            ("fallback", "settled"),
        }
        assert len({row["attempt_id"] for row in records}) == 2


async def test_stream_disconnect_retains_unknown_cost(redis_server: RedisServer) -> None:
    database_url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    key = f"gateway-cost-test-{uuid.uuid4()}"
    principal_id = _key_id(key)
    upstream_body = ControlledStream(sse_body(openai_chunk("partial"), done=False))
    stub = UpstreamStub(lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=upstream_body))
    raw: dict[str, object] = {
        "providers": {"mock": {"type": "openai_compatible", "base_url": "https://fake.test/v1"}},
        "models": {"fast": {"provider": "mock", "model": "model", "max_output_tokens": 100}},
        "admission": {"enabled": True, "default_limits": {"max_concurrent_requests": 1}},
        "cost": {
            "enabled": True,
            "default_key_limits": {"daily_micro_usd": 100},
            "pricing": [
                {
                    "provider": "mock",
                    "model": "model",
                    "version": "v1",
                    "effective_at": "2020-01-01T00:00:00Z",
                    "input_per_million_usd": "0",
                    "output_per_million_usd": "1",
                }
            ],
        },
    }
    app = build_app(
        make_settings(redis_url=redis_server.url, database_url=database_url, api_keys=(key,)),
        make_config(raw, {"mock": None}),
        partial(build_provider, transport=stub),
    )
    async with (
        run_server(app) as url,
        httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {key}"}) as client,
        asyncio.timeout(5),
    ):
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if '"partial"' in line:
                    break
        await upstream_body.closed.wait()

        async def unresolved() -> bool:
            async with app.state.cost_store.engine.connect() as connection:
                result = await connection.execute(
                    text("""SELECT state, charge_micro_usd
                    FROM cost_attempts WHERE principal_id=:principal"""),
                    {"principal": principal_id},
                )
                return bool(result.one_or_none() == ("unresolved", 100))

        await eventually(unresolved)


async def test_two_gateway_instances_do_not_over_admit_shared_budget(
    redis_server: RedisServer,
) -> None:
    database_url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    key = f"gateway-cost-test-{uuid.uuid4()}"
    raw: dict[str, object] = {
        "providers": {"fake": {"type": "openai_compatible", "base_url": "https://fake.test/v1"}},
        "models": {"fast": {"provider": "fake", "model": "mock", "max_output_tokens": 100}},
        "admission": {"enabled": True, "default_limits": {"max_concurrent_requests": 100}},
        "cost": {
            "enabled": True,
            "default_key_limits": {"daily_micro_usd": 100},
            "pricing": [
                {
                    "provider": "fake",
                    "model": "mock",
                    "version": "v1",
                    "effective_at": "2020-01-01T00:00:00Z",
                    "input_per_million_usd": "0",
                    "output_per_million_usd": "1",
                }
            ],
        },
    }
    providers = [FakeProvider(), FakeProvider()]
    apps = [
        build_app(
            make_settings(redis_url=redis_server.url, database_url=database_url, api_keys=(key,)),
            make_config(raw, {"fake": None}),
            lambda *_, provider=provider: provider,
        )
        for provider in providers
    ]
    async with serve(apps[0]) as first, serve(apps[1]) as second:

        async def post(client: httpx.AsyncClient) -> int:
            result = await client.post(
                "/v1/chat/completions",
                json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {key}"},
            )
            return result.status_code

        statuses = await asyncio.gather(*(post(first if i % 2 else second) for i in range(20)))
    assert statuses.count(200) == 1
    assert statuses.count(429) == 19
    assert sum(len(provider.requests) for provider in providers) == 1
