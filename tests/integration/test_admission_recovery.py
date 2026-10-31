"""Recovery when an admission's outcome is unknown to the gateway."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from prometheus_client import generate_latest
from pydantic import SecretStr
from redis.asyncio import Redis

from gateway.admission.controller import (
    READINESS_CACHE_SECONDS,
    AdmissionController,
    AdmissionStore,
)
from gateway.admission.limits import KeyLimits, TokenBucket
from gateway.admission.redis_store import (
    KEY_PREFIX,
    Admission,
    AdmissionDecision,
    RedisAdmissionStore,
    SettleOutcome,
)
from gateway.admission.usage import UsageLedger
from gateway.core.config import AdmissionConfig
from gateway.core.metrics import Metrics
from gateway.core.redis import create_redis_client
from gateway.core.security import Principal
from gateway.errors import AdmissionUnavailableError
from gateway.providers.base import Usage
from gateway.resilience.deadline import Deadline
from tests.redis_proxy import ReplyDroppingProxy
from tests.redis_server import RedisServer

PRINCIPAL = Principal(id="key-0123456789ab")
STATE = f"{KEY_PREFIX}:{{{PRINCIPAL.id}}}"
LIMITS = KeyLimits(requests=TokenBucket(1, 10), tokens=TokenBucket(1, 1000), max_concurrent=2)


def controller_for(store: AdmissionStore) -> AdmissionController:
    return AdmissionController(
        store,
        AdmissionConfig(enabled=True),
        request_deadline_seconds=60,
        metrics=Metrics(),
    )


@asynccontextmanager
async def proxied_controller(proxy: ReplyDroppingProxy) -> AsyncIterator[AdmissionController]:
    """A controller connected through the proxy; waits for its background settlements
    and closes its connections on exit, including when the test fails."""
    client = create_redis_client(SecretStr(proxy.url), AdmissionConfig())
    controller = controller_for(RedisAdmissionStore(client))
    try:
        yield controller
    finally:
        await controller.aclose()
        await client.aclose(close_connection_pool=True)


@pytest.fixture
async def proxy(redis_server: RedisServer) -> AsyncIterator[ReplyDroppingProxy]:
    proxy = ReplyDroppingProxy(redis_server.socket_path)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.stop()


async def test_lost_admission_reply_is_refused_and_the_reservation_released(
    proxy: ReplyDroppingProxy, redis_client: Redis
) -> None:
    # Load both scripts through a direct connection (under another key). Otherwise
    # the proxied calls would fail with NOSCRIPT, a reply the proxy drops, and never run.
    warmup = Admission("key-ffffffffffff", "0" * 32, LIMITS, 0, lease_ttl_ms=1, record_ttl_ms=1)
    direct = RedisAdmissionStore(redis_client)
    await direct.admit(warmup)
    await direct.settle(warmup, 0)
    proxy.drop_script_replies = True

    async with proxied_controller(proxy) as controller:
        with pytest.raises(AdmissionUnavailableError):
            await controller.admit(PRINCIPAL, LIMITS, tokens=400, deadline=Deadline(10))

    # The admission and the release each ran (and were repeated once after the broken
    # connection) although no reply arrived. The repeats did not charge twice.
    assert proxy.dropped_replies == 4
    assert await redis_client.zcard(f"{STATE}:leases") == 0
    [reservation] = [key async for key in redis_client.scan_iter(f"{STATE}:reservation:*")]
    assert await redis_client.hget(reservation, "state") == "settled"
    assert float(await redis_client.hget(f"{STATE}:tokens", "tokens") or 0) == pytest.approx(
        1000, abs=2
    )


async def test_a_silently_broken_connection_is_replaced_without_refusing_the_request(
    proxy: ReplyDroppingProxy, redis_client: Redis
) -> None:
    async with proxied_controller(proxy) as controller:
        first = await controller.admit(PRINCIPAL, LIMITS, tokens=100, deadline=Deadline(10))
        await controller.settle(first, UsageLedger())

        proxy.break_open_connections()
        second = await controller.admit(PRINCIPAL, LIMITS, tokens=100, deadline=Deadline(10))
        proxy.break_open_connections()
        await controller.settle(second, UsageLedger())

    assert await redis_client.zcard(f"{STATE}:leases") == 0
    # Each request was charged once despite the repeated calls.
    assert float(await redis_client.hget(f"{STATE}:tokens", "tokens") or 0) == pytest.approx(
        1000, abs=2
    )


class BlockingStore:
    """A store whose admission call never returns, and which records settlements."""

    def __init__(self) -> None:
        self.admit_started = asyncio.Event()
        self.settled: list[tuple[str, int]] = []

    async def admit(self, admission: Admission) -> AdmissionDecision:
        self.admit_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def settle(self, admission: Admission, charged_tokens: int) -> SettleOutcome:
        self.settled.append((admission.reservation_id, charged_tokens))
        return SettleOutcome.MISSING

    async def ping(self) -> None:
        return


async def test_cancellation_during_admission_releases_the_reservation() -> None:
    store = BlockingStore()
    controller = controller_for(store)

    admit = asyncio.create_task(
        controller.admit(PRINCIPAL, LIMITS, tokens=100, deadline=Deadline(10))
    )
    await store.admit_started.wait()
    admit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admit
    await controller.aclose()

    [(_, charged)] = store.settled
    assert charged == 0


async def test_settlement_completes_even_if_the_caller_is_cancelled(
    redis_client: Redis,
) -> None:
    controller = controller_for(RedisAdmissionStore(redis_client))
    admission = await controller.admit(PRINCIPAL, LIMITS, tokens=100, deadline=Deadline(10))

    settle = asyncio.create_task(controller.settle(admission, UsageLedger()))
    await asyncio.sleep(0)
    settle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settle
    await controller.aclose()

    assert await redis_client.zcard(f"{STATE}:leases") == 0


class CountingStore(BlockingStore):
    def __init__(self) -> None:
        super().__init__()
        self.pings = 0

    async def ping(self) -> None:
        self.pings += 1
        await asyncio.sleep(0)


async def test_readiness_checks_are_reused_for_a_second() -> None:
    store = CountingStore()
    now = [100.0]
    controller = AdmissionController(
        store,
        AdmissionConfig(enabled=True),
        request_deadline_seconds=60,
        metrics=Metrics(),
        clock=lambda: now[0],
    )

    results = await asyncio.gather(*(controller.is_ready() for _ in range(50)))
    now[0] += READINESS_CACHE_SECONDS
    await controller.is_ready()

    assert all(results)
    assert store.pings == 2


async def test_slow_redis_cannot_create_unbounded_settlement_tasks() -> None:
    class SlowStore(CountingStore):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def settle(self, admission: Admission, charged_tokens: int) -> SettleOutcome:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return SettleOutcome.SETTLED

    store = SlowStore()
    controller = AdmissionController(
        store,
        AdmissionConfig(enabled=True, redis_max_connections=1),
        request_deadline_seconds=60,
        metrics=Metrics(),
    )
    reserved = Admission(PRINCIPAL.id, "a" * 32, LIMITS, 100, 60_000, 120_000)
    first = asyncio.create_task(controller.settle(reserved, UsageLedger()))
    await store.started.wait()
    # Excess cleanup retains the reservation instead of creating more tasks.
    await asyncio.gather(*(controller.settle(reserved, UsageLedger()) for _ in range(100)))
    with pytest.raises(AdmissionUnavailableError, match="cleanup capacity"):
        await controller.admit(PRINCIPAL, LIMITS, tokens=100, deadline=Deadline(10))
    assert store.calls == 1
    store.release.set()
    await first
    await controller.aclose()


async def test_duplicate_settlement_does_not_double_count_charged_token_metrics(
    redis_client: Redis,
) -> None:
    metrics = Metrics()
    controller = AdmissionController(
        RedisAdmissionStore(redis_client),
        AdmissionConfig(enabled=True),
        request_deadline_seconds=60,
        metrics=metrics,
    )
    reserved = await controller.admit(PRINCIPAL, LIMITS, 100, Deadline(10))
    ledger = UsageLedger()
    ledger.start("provider").succeeded(Usage(10, 5, 15))
    await controller.settle(reserved, ledger)
    await controller.settle(reserved, ledger)
    await controller.aclose()
    assert (
        'gateway_charged_tokens_total{usage="observed"} 15.0'
        in generate_latest(metrics.registry).decode()
    )
