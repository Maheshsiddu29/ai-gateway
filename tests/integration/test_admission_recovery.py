"""Recovery when an admission's outcome is unknown to the gateway."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from gateway.admission.controller import AdmissionController, AdmissionStore
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
    proxied = create_redis_client(SecretStr(proxy.url), AdmissionConfig())
    controller = controller_for(RedisAdmissionStore(proxied))
    proxy.drop_script_replies = True

    with pytest.raises(AdmissionUnavailableError):
        await controller.admit(PRINCIPAL, LIMITS, tokens=400, deadline=Deadline(10))
    await controller.aclose()
    await proxied.aclose(close_connection_pool=True)

    # Both the admission and the release ran in Redis although neither reply arrived.
    assert proxy.dropped_replies == 2
    assert await redis_client.zcard(f"{STATE}:leases") == 0
    [reservation] = [key async for key in redis_client.scan_iter(f"{STATE}:reservation:*")]
    assert await redis_client.hget(reservation, "state") == "settled"
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
