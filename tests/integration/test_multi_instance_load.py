"""Limits under concurrent load across two gateway instances sharing one Redis.

Two gateway servers and the mock provider run in uvicorn on real sockets. Each
gateway key calls a different model, so the mock's per-model peak concurrency is
the key's peak number of requests admitted at once.

These are correctness checks, not benchmarks; see tools/loadtest.py for those.
"""

import asyncio
import itertools
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis

from gateway.admission.redis_store import KEY_PREFIX
from gateway.admission.tokens import estimate_input_tokens
from gateway.core.security import StaticKeyAuthenticator
from gateway.main import build_app
from gateway.schemas.completions import ChatMessage
from mock_upstream import create_app as create_mock_app
from tests.fakes import make_config, make_settings
from tests.redis_server import RedisServer
from tests.servers import run_server

KEYS = {"a": "load-test-key-a-0123456789abcdef", "b": "load-test-key-b-0123456789abcdef"}
REQUESTS_PER_KEY = 150
CLIENT_CONCURRENCY_PER_KEY = 50
MAX_TOKENS = 64
PROMPT = "one two three"
RESERVED = estimate_input_tokens([ChatMessage(role="user", content=PROMPT)]) + MAX_TOKENS


def principal_id(key: str) -> str:
    principal = StaticKeyAuthenticator([SecretStr(key)]).authenticate(key)
    assert principal is not None
    return principal.id


@asynccontextmanager
async def two_gateways(
    redis_server: RedisServer, limits_by_key: dict[str, dict[str, int]]
) -> AsyncIterator[tuple[list[str], httpx.AsyncClient]]:
    async with AsyncExitStack() as stack:
        mock_url = await stack.enter_async_context(
            run_server(create_mock_app(stream_delay_seconds=0, response_delay_seconds=0.05))
        )
        config = {
            "providers": {
                "mock": {
                    "type": "openai_compatible",
                    "base_url": f"{mock_url}/v1",
                    "api_key_env": "MOCK_KEY",
                }
            },
            "models": {
                alias: {
                    "provider": "mock",
                    "model": f"model-{alias}",
                    "max_output_tokens": MAX_TOKENS,
                }
                for alias in KEYS
            },
            "admission": {
                "enabled": True,
                "key_limits": {
                    principal_id(KEYS[alias]): limits for alias, limits in limits_by_key.items()
                },
            },
        }
        settings = make_settings(redis_url=redis_server.url, api_keys=tuple(KEYS.values()))
        gateway_urls = [
            await stack.enter_async_context(
                run_server(build_app(settings, make_config(config, {"mock": "mock-key"})))
            )
            for _ in range(2)
        ]
        limits = httpx.Limits(max_connections=4 * CLIENT_CONCURRENCY_PER_KEY)
        client = await stack.enter_async_context(httpx.AsyncClient(limits=limits, timeout=30))
        yield [*gateway_urls, mock_url], client


async def drive(client: httpx.AsyncClient, gateway_urls: list[str], alias: str) -> Counter[int]:
    """Send REQUESTS_PER_KEY requests for one key, alternating between instances."""
    semaphore = asyncio.Semaphore(CLIENT_CONCURRENCY_PER_KEY)
    instances = itertools.cycle(gateway_urls)
    headers = {"Authorization": f"Bearer {KEYS[alias]}"}
    body = {"model": alias, "messages": [{"role": "user", "content": PROMPT}]}

    async def one(url: str) -> int:
        async with semaphore:
            response = await client.post(f"{url}/v1/chat/completions", json=body, headers=headers)
            return response.status_code

    statuses = await asyncio.gather(*(one(next(instances)) for _ in range(REQUESTS_PER_KEY)))
    return Counter(statuses)


async def assert_no_leaked_state(redis: Redis) -> None:
    leases = [key async for key in redis.scan_iter(f"{KEY_PREFIX}:*:leases")]
    assert [await redis.zcard(key) for key in leases] == [0] * len(leases)
    records = [key async for key in redis.scan_iter(f"{KEY_PREFIX}:*:reservation:*")]
    states = Counter([await redis.hget(key, "state") for key in records])
    assert set(states) == {"settled"}


async def test_concurrency_limit_holds_under_load(
    redis_server: RedisServer, redis_client: Redis
) -> None:
    limit = 4

    async with two_gateways(
        redis_server, {alias: {"max_concurrent_requests": limit} for alias in KEYS}
    ) as (urls, client):
        *gateway_urls, mock_url = urls
        results = await asyncio.gather(*(drive(client, gateway_urls, alias) for alias in KEYS))
        stats = (await client.get(f"{mock_url}/stats")).json()

    for alias, statuses in zip(KEYS, results, strict=True):
        assert set(statuses) <= {200, 429}
        assert sum(statuses.values()) == REQUESTS_PER_KEY
        assert 1 <= stats[f"model-{alias}"]["peak_concurrent"] <= limit
        # Every admitted request reached the provider and nothing else did.
        assert stats[f"model-{alias}"]["requests"] == statuses[200]
    await assert_no_leaked_state(redis_client)


async def test_request_and_token_limits_hold_under_load(
    redis_server: RedisServer, redis_client: Redis
) -> None:
    request_burst = 40
    concurrent_reservations = 5
    limits = {
        # One request per minute refills nothing within the test.
        "a": {"requests_per_minute": 1, "request_burst": request_burst},
        "b": {"tokens_per_minute": 1, "token_burst": concurrent_reservations * RESERVED},
    }

    async with two_gateways(redis_server, limits) as (urls, client):
        *gateway_urls, mock_url = urls
        requests_a, tokens_b = await asyncio.gather(
            drive(client, gateway_urls, "a"), drive(client, gateway_urls, "b")
        )
        stats = (await client.get(f"{mock_url}/stats")).json()

    assert requests_a[200] == request_burst
    assert requests_a[429] == REQUESTS_PER_KEY - request_burst
    # Reported usage is far below the reservation, so settlement refunds tokens and
    # more than five requests succeed over time; but no more than five reservations
    # can be held at once.
    assert stats["model-b"]["peak_concurrent"] <= concurrent_reservations
    assert tokens_b[200] >= concurrent_reservations
    assert stats["model-b"]["requests"] == tokens_b[200]
    balance = await redis_client.hget(
        f"{KEY_PREFIX}:{{{principal_id(KEYS['b'])}}}:tokens", "tokens"
    )
    assert balance is not None
    assert float(balance) <= concurrent_reservations * RESERVED
    await assert_no_leaked_state(redis_client)
