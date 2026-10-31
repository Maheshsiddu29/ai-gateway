"""Admission scripts against a real Redis server.

Separate Redis clients stand in for separate gateway instances: they share nothing
but the Redis server, as independent processes would.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway.admission.limits import KeyLimits, TokenBucket
from gateway.admission.redis_store import (
    KEY_PREFIX,
    Admission,
    RedisAdmissionStore,
    Rejection,
    SettleOutcome,
)
from gateway.core.config import AdmissionConfig
from gateway.core.redis import create_redis_client
from tests.redis_server import RedisServer

PRINCIPAL = "key-0123456789ab"
OTHER_PRINCIPAL = "key-ba9876543210"
UNLIMITED = KeyLimits(requests=None, tokens=None, max_concurrent=None)


def admission(
    limits: KeyLimits,
    *,
    tokens: int = 0,
    principal: str = PRINCIPAL,
    lease_ttl_ms: int = 60_000,
    record_ttl_ms: int = 120_000,
) -> Admission:
    return Admission(
        principal_id=principal,
        reservation_id=uuid.uuid4().hex,
        limits=limits,
        tokens=tokens,
        lease_ttl_ms=lease_ttl_ms,
        record_ttl_ms=record_ttl_ms,
    )


def requests_only(rate: int, capacity: int) -> KeyLimits:
    return KeyLimits(requests=TokenBucket(rate, capacity), tokens=None, max_concurrent=None)


def tokens_only(rate: int, capacity: int) -> KeyLimits:
    return KeyLimits(requests=None, tokens=TokenBucket(rate, capacity), max_concurrent=None)


async def bucket_tokens(redis: Redis, name: str, principal: str = PRINCIPAL) -> float | None:
    value = await redis.hget(f"{KEY_PREFIX}:{{{principal}}}:{name}", "tokens")
    return None if value is None else float(value)


@pytest.fixture
async def store(redis_client: Redis) -> RedisAdmissionStore:
    return RedisAdmissionStore(redis_client)


@pytest.fixture
async def other_instance(redis_server: RedisServer) -> AsyncIterator[RedisAdmissionStore]:
    client = create_redis_client(SecretStr(redis_server.url), AdmissionConfig())
    try:
        yield RedisAdmissionStore(client)
    finally:
        await client.aclose(close_connection_pool=True)


class TestRequestBucket:
    async def test_allows_the_burst_then_rejects_with_retry_after(
        self, store: RedisAdmissionStore
    ) -> None:
        limits = requests_only(rate=60, capacity=3)

        decisions = [await store.admit(admission(limits)) for _ in range(4)]

        assert [d.admitted for d in decisions] == [True, True, True, False]
        assert decisions[3].rejection is Rejection.REQUESTS
        # One request per second refills; at most a full second remains.
        assert decisions[3].retry_after_ms is not None
        assert 0 < decisions[3].retry_after_ms <= 1000

    async def test_refills_over_time(self, store: RedisAdmissionStore) -> None:
        limits = requests_only(rate=6000, capacity=1)

        assert (await store.admit(admission(limits))).admitted
        assert not (await store.admit(admission(limits))).admitted
        await asyncio.sleep(0.05)

        assert (await store.admit(admission(limits))).admitted

    async def test_concurrent_admissions_from_two_instances_admit_exactly_capacity(
        self, store: RedisAdmissionStore, other_instance: RedisAdmissionStore
    ) -> None:
        limits = requests_only(rate=1, capacity=10)
        attempts = [
            instance.admit(admission(limits))
            for _ in range(25)
            for instance in (store, other_instance)
        ]

        decisions = await asyncio.gather(*attempts)

        assert sum(d.admitted for d in decisions) == 10


class TestTokenBucket:
    async def test_rejects_when_the_reservation_does_not_fit(
        self, store: RedisAdmissionStore
    ) -> None:
        limits = tokens_only(rate=1000, capacity=1000)

        first = await store.admit(admission(limits, tokens=600))
        second = await store.admit(admission(limits, tokens=600))

        assert first.admitted
        assert second.rejection is Rejection.TOKENS
        # 200 tokens are missing at 1000 per minute: about 12 seconds.
        assert second.retry_after_ms is not None
        assert 11_000 < second.retry_after_ms <= 12_000

    async def test_settlement_refunds_unused_tokens_exactly_once(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = tokens_only(rate=1, capacity=1000)
        reserved = admission(limits, tokens=800)
        await store.admit(reserved)

        outcomes = await asyncio.gather(*(store.settle(reserved, 300) for _ in range(10)))

        assert outcomes.count(SettleOutcome.SETTLED) == 1
        assert outcomes.count(SettleOutcome.DUPLICATE) == 9
        balance = await bucket_tokens(redis_client, "tokens")
        assert balance == pytest.approx(700, abs=1)

    async def test_usage_above_the_reservation_becomes_debt(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = tokens_only(rate=60, capacity=1000)
        reserved = admission(limits, tokens=500)
        await store.admit(reserved)

        await store.settle(reserved, 1200)

        balance = await bucket_tokens(redis_client, "tokens")
        assert balance == pytest.approx(-200, abs=1)
        later = await store.admit(admission(limits, tokens=1))
        assert later.rejection is Rejection.TOKENS
        # 201 tokens missing at one per second.
        assert later.retry_after_ms is not None
        assert later.retry_after_ms > 200_000

    async def test_all_excess_usage_remains_debt(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = tokens_only(rate=60, capacity=1000)
        reserved = admission(limits, tokens=10)
        await store.admit(reserved)

        await store.settle(reserved, 1_000_000)

        assert await bucket_tokens(redis_client, "tokens") == pytest.approx(-999_000, abs=1)


class TestConcurrency:
    async def test_limit_is_shared_across_instances_and_released_by_settlement(
        self, store: RedisAdmissionStore, other_instance: RedisAdmissionStore
    ) -> None:
        limits = KeyLimits(requests=None, tokens=None, max_concurrent=5)
        attempts = [admission(limits) for _ in range(40)]

        decisions = await asyncio.gather(
            *(
                (store if index % 2 else other_instance).admit(attempt)
                for index, attempt in enumerate(attempts)
            )
        )

        admitted = [a for a, d in zip(attempts, decisions, strict=True) if d.admitted]
        assert len(admitted) == 5
        assert {d.rejection for d in decisions if not d.admitted} == {Rejection.CONCURRENCY}
        await other_instance.settle(admitted[0], 0)
        assert (await store.admit(admission(limits))).admitted

    async def test_leases_of_crashed_requests_expire(self, store: RedisAdmissionStore) -> None:
        limits = KeyLimits(requests=None, tokens=None, max_concurrent=1)

        # Admitted and never settled, as if the gateway process died.
        assert (await store.admit(admission(limits, lease_ttl_ms=50))).admitted
        assert not (await store.admit(admission(limits))).admitted
        await asyncio.sleep(0.1)

        assert (await store.admit(admission(limits))).admitted


class TestAtomicity:
    async def test_a_token_rejection_consumes_no_request_quota_or_lease(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = KeyLimits(
            requests=TokenBucket(1, 3), tokens=TokenBucket(1, 100), max_concurrent=None
        )

        rejected = await store.admit(admission(limits, tokens=150))

        assert rejected.rejection is Rejection.TOKENS
        assert await bucket_tokens(redis_client, "requests") is None
        assert await redis_client.zcard(f"{KEY_PREFIX}:{{{PRINCIPAL}}}:leases") == 0
        decisions = [await store.admit(admission(limits, tokens=10)) for _ in range(3)]
        assert all(d.admitted for d in decisions)

    async def test_a_concurrency_rejection_consumes_no_rate_quota(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = KeyLimits(
            requests=TokenBucket(1, 10), tokens=TokenBucket(1, 1000), max_concurrent=1
        )
        assert (await store.admit(admission(limits, tokens=100))).admitted
        requests_before = await bucket_tokens(redis_client, "requests")
        tokens_before = await bucket_tokens(redis_client, "tokens")

        rejected = await store.admit(admission(limits, tokens=100))

        assert rejected.rejection is Rejection.CONCURRENCY
        assert await bucket_tokens(redis_client, "requests") == requests_before
        assert await bucket_tokens(redis_client, "tokens") == tokens_before

    async def test_repeating_an_admission_does_not_consume_twice(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = tokens_only(rate=1, capacity=1000)
        reserved = admission(limits, tokens=400)

        first = await store.admit(reserved)
        repeated = await store.admit(reserved)

        assert first.admitted
        assert repeated.admitted
        assert await bucket_tokens(redis_client, "tokens") == pytest.approx(600, abs=1)

    async def test_settlement_before_admission_prevents_it_from_committing(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        # The order seen when an admission reply is lost: the gateway releases the
        # reservation, and the admission command reaches Redis afterwards.
        limits = KeyLimits(
            requests=TokenBucket(1, 5), tokens=TokenBucket(1, 1000), max_concurrent=2
        )
        reserved = admission(limits, tokens=100)

        assert await store.settle(reserved, 0) is SettleOutcome.MISSING
        late = await store.admit(reserved)

        assert late.rejection is Rejection.RELEASED
        assert await bucket_tokens(redis_client, "requests") is None
        assert await bucket_tokens(redis_client, "tokens") is None
        assert await store.settle(reserved, 0) is SettleOutcome.DUPLICATE

    async def test_an_expired_reservation_is_not_refunded(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = tokens_only(rate=1, capacity=1000)
        reserved = admission(limits, tokens=400, lease_ttl_ms=50, record_ttl_ms=50)
        await store.admit(reserved)
        await asyncio.sleep(0.1)

        assert await store.settle(reserved, 0) is SettleOutcome.MISSING
        assert await bucket_tokens(redis_client, "tokens") == pytest.approx(600, abs=1)


class TestIsolationAndRetention:
    async def test_keys_have_independent_limits(self, store: RedisAdmissionStore) -> None:
        limits = requests_only(rate=1, capacity=1)

        assert (await store.admit(admission(limits))).admitted
        assert not (await store.admit(admission(limits))).admitted

        assert (await store.admit(admission(limits, principal=OTHER_PRINCIPAL))).admitted

    async def test_all_state_has_an_expiry(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = KeyLimits(
            requests=TokenBucket(60, 5), tokens=TokenBucket(60, 500), max_concurrent=3
        )
        reserved = admission(limits, tokens=50)
        await store.admit(reserved)
        await store.settle(reserved, 20)

        keys = [key async for key in redis_client.scan_iter(f"{KEY_PREFIX}:*")]

        # The leases set is deleted once its last member is removed.
        assert len(keys) == 3
        ttls = [await redis_client.pttl(key) for key in keys]
        assert all(ttl > 0 for ttl in ttls)

    async def test_full_buckets_expire(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        await store.admit(admission(requests_only(rate=60_000, capacity=1)))

        # Refilled after one millisecond, plus the one second of slack.
        await asyncio.sleep(1.2)

        assert await bucket_tokens(redis_client, "requests") is None

    async def test_rejects_ids_that_could_escape_the_key_namespace(
        self, store: RedisAdmissionStore
    ) -> None:
        bad = dataclasses.replace(admission(UNLIMITED), principal_id="key-}x{")

        with pytest.raises(ValueError, match="principal"):
            await store.admit(bad)


class TestServerFailures:
    async def test_scripts_are_reloaded_after_the_script_cache_is_flushed(
        self, store: RedisAdmissionStore, redis_client: Redis
    ) -> None:
        limits = requests_only(rate=1, capacity=2)
        assert (await store.admit(admission(limits))).admitted

        await redis_client.script_flush()

        assert (await store.admit(admission(limits))).admitted
        assert not (await store.admit(admission(limits))).admitted

    async def test_unavailable_server_fails_fast_and_recovers_after_restart(
        self, redis_server: RedisServer, store: RedisAdmissionStore
    ) -> None:
        limits = requests_only(rate=1, capacity=1)
        assert (await store.admit(admission(limits))).admitted

        await redis_server.stop()
        async with asyncio.timeout(2):
            with pytest.raises(RedisConnectionError):
                await store.admit(admission(limits))
        await redis_server.start()

        # The restarted server has no persistence, so the exhausted bucket is gone.
        assert (await store.admit(admission(limits))).admitted


async def test_settled_reservation_cannot_authorize_another_dispatch(
    store: RedisAdmissionStore,
) -> None:
    reserved = admission(UNLIMITED)
    assert (await store.admit(reserved)).admitted
    await store.settle(reserved, 0)
    assert (await store.admit(reserved)).rejection is Rejection.RELEASED


async def test_expired_lease_cannot_be_replayed(
    store: RedisAdmissionStore,
    redis_client: Redis,
) -> None:
    reserved = admission(UNLIMITED)
    await store.admit(reserved)
    await redis_client.zadd(f"{KEY_PREFIX}:{{{PRINCIPAL}}}:leases", {reserved.reservation_id: 0})
    assert (await store.admit(reserved)).rejection is Rejection.RELEASED


async def test_backward_clock_does_not_create_a_second_refill(
    store: RedisAdmissionStore,
    redis_client: Redis,
) -> None:
    limits = requests_only(rate=60, capacity=10)
    key = f"{KEY_PREFIX}:{{{PRINCIPAL}}}:requests"
    seconds, micros = await redis_client.time()
    future = seconds * 1000 + micros // 1000 + 60_000
    await redis_client.hset(key, mapping={"tokens": 5, "updated_ms": future})
    assert (await store.admit(admission(limits))).admitted
    assert int(await redis_client.hget(key, "updated_ms") or 0) == future
    assert await bucket_tokens(redis_client, "requests") == 4
