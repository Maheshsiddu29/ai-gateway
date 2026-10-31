"""Admission control around the Redis store: failure policy, recovery, and telemetry.

Failure policy: any Redis error or timeout while admitting refuses the request with
``admission_unavailable``. There is no local fallback, so a Redis outage never
silently disables limits.

Broken connections: a pooled connection can break without the client noticing (for
example when Redis restarts behind a container network), so its next command fails.
Both scripts are idempotent for a reservation ID, so after a connection error the
call is repeated once, with the same ID, on a new connection, within the same
operation timeout. A repeated admission that had already committed reports itself as
admitted without charging again.

Lost replies: if an admission call still fails, or is cancelled, after the command was
sent, it may have committed. The controller then settles the reservation with
zero tokens. Settlement is idempotent and, if it runs before a delayed admission,
prevents that admission from committing, so either way no lease is left behind.
If the release also fails, the lease expires on its own and the reserved tokens stay
charged.

Settlement runs in a task shielded from cancellation, so a client disconnect during
settlement cannot leave it half done. Settlement tasks are tracked, bounded by the
Redis operation timeout, and awaited on shutdown.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from math import ceil
from typing import Any, Protocol

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from gateway.admission.limits import KeyLimits
from gateway.admission.redis_store import (
    Admission,
    AdmissionDecision,
    Rejection,
    SettleOutcome,
)
from gateway.admission.usage import UsageLedger
from gateway.core.config import AdmissionConfig
from gateway.core.metrics import Metrics
from gateway.core.security import Principal
from gateway.errors import AdmissionUnavailableError, DeadlineExceededError, RateLimitedError
from gateway.resilience.deadline import Deadline

logger = logging.getLogger(__name__)

_REDIS_FAILURES = (RedisError, TimeoutError, OSError)
READINESS_CACHE_SECONDS = 1.0
_REJECTIONS = {
    Rejection.REQUESTS: ("requests_per_minute_exceeded", "Request rate limit exceeded."),
    Rejection.TOKENS: ("tokens_per_minute_exceeded", "Token rate limit exceeded."),
    Rejection.CONCURRENCY: ("concurrency_limit_exceeded", "Too many concurrent requests."),
    # Only possible if a release for this reservation ID raced ahead of the
    # admission, which the gateway never does for a request it is still serving.
    Rejection.RELEASED: ("reservation_released", "The request could not be admitted."),
}


class AdmissionStore(Protocol):
    """Implemented by RedisAdmissionStore."""

    async def admit(self, admission: Admission) -> AdmissionDecision: ...

    async def settle(self, admission: Admission, charged_tokens: int) -> SettleOutcome: ...

    async def ping(self) -> None: ...


class AdmissionController:
    def __init__(
        self,
        store: AdmissionStore,
        config: AdmissionConfig,
        *,
        request_deadline_seconds: float,
        metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._config = config
        self._metrics = metrics
        self._timeout = config.redis_operation_timeout_seconds
        # A lease must outlive any request that holds it, and requests cannot
        # outlive their deadline, so no lease renewal is needed.
        self._lease_ttl_ms = ceil((request_deadline_seconds + config.lease_grace_seconds) * 1000)
        self._record_ttl_ms = self._lease_ttl_ms + ceil(config.reservation_retention_seconds * 1000)
        self._limits: dict[str, KeyLimits] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._clock = clock
        self._readiness: tuple[float, bool] | None = None

    def limits_for(self, principal: Principal) -> KeyLimits:
        limits = self._limits.get(principal.id)
        if limits is None:
            limits = KeyLimits.from_config(self._config.limits_for(principal.id))
            self._limits[principal.id] = limits
        return limits

    async def admit(
        self, principal: Principal, limits: KeyLimits, tokens: int, deadline: Deadline
    ) -> Admission:
        admission = Admission(
            principal_id=principal.id,
            reservation_id=uuid.uuid4().hex,
            limits=limits,
            tokens=tokens,
            lease_ttl_ms=self._lease_ttl_ms,
            record_ttl_ms=self._record_ttl_ms,
        )
        started = time.perf_counter()
        try:
            async with deadline.limit(), asyncio.timeout(self._timeout):
                decision = await _with_reconnect(lambda: self._store.admit(admission))
        except (*_REDIS_FAILURES, DeadlineExceededError, asyncio.CancelledError) as exc:
            self._release_unconfirmed(admission)
            if isinstance(exc, DeadlineExceededError | asyncio.CancelledError):
                raise
            self._metrics.redis_errors.labels("admit").inc()
            self._metrics.admission_decisions.labels("unavailable").inc()
            logger.warning(
                "admission unavailable",
                extra={"redis_error": type(exc).__name__, **_identifiers(admission)},
            )
            raise AdmissionUnavailableError(
                "Request admission is temporarily unavailable."
            ) from None
        finally:
            self._metrics.admission_duration.observe(time.perf_counter() - started)

        self._record(admission, decision)
        if decision.rejection is not None:
            code, message = _REJECTIONS[decision.rejection]
            retry_after = decision.retry_after_ms
            raise RateLimitedError(
                message,
                code=code,
                retry_after_seconds=ceil(retry_after / 1000) if retry_after is not None else None,
            )
        self._metrics.reserved_tokens.inc(tokens)
        return admission

    async def settle(self, admission: Admission, ledger: UsageLedger) -> None:
        """Release the lease and charge the tokens used. Never raises."""
        await asyncio.shield(self._spawn(self._settle(admission, ledger)))

    async def is_ready(self) -> bool:
        # The readiness endpoint is unauthenticated, so the result is reused briefly:
        # otherwise every probe request would cost a Redis round trip.
        now = self._clock()
        if self._readiness is not None and now - self._readiness[0] < READINESS_CACHE_SECONDS:
            return self._readiness[1]
        try:
            async with asyncio.timeout(self._timeout):
                await self._store.ping()
        except _REDIS_FAILURES:
            self._metrics.redis_errors.labels("ping").inc()
            ready = False
        else:
            ready = True
        self._readiness = (now, ready)
        return ready

    async def aclose(self) -> None:
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _release_unconfirmed(self, admission: Admission) -> None:
        self._spawn(self._settle(admission, UsageLedger()))

    async def _settle(self, admission: Admission, ledger: UsageLedger) -> None:
        charge = ledger.charge(admission.tokens)
        fields = {
            **_identifiers(admission),
            "reserved_tokens": admission.tokens,
            "charged_tokens": charge.tokens,
            "usage": charge.status.value,
            "attempts": len(ledger.attempts),
        }
        try:
            async with asyncio.timeout(self._timeout):
                outcome = await _with_reconnect(
                    lambda: self._store.settle(admission, charge.tokens)
                )
        except _REDIS_FAILURES as exc:
            self._metrics.redis_errors.labels("settle").inc()
            self._metrics.reservations_settled.labels("failed").inc()
            logger.warning(
                "reservation unresolved",
                extra={**fields, "redis_error": type(exc).__name__},
            )
            return
        except Exception:
            # Runs detached from the request, so log rather than lose the error.
            logger.exception("reservation settlement failed", extra=fields)
            return
        self._metrics.reservations_settled.labels(outcome.value).inc()
        self._metrics.charged_tokens.labels(charge.status.value).inc(charge.tokens)
        logger.info("reservation settled", extra={**fields, "outcome": outcome.value})

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _record(self, admission: Admission, decision: AdmissionDecision) -> None:
        outcome = "accepted" if decision.admitted else f"{decision.rejection}_rejected"
        self._metrics.admission_decisions.labels(outcome).inc()
        logger.info(
            "admission decision",
            extra={
                **_identifiers(admission),
                "outcome": outcome,
                "reserved_tokens": admission.tokens,
                "retry_after_ms": decision.retry_after_ms,
            },
        )


async def _with_reconnect[T](call: Callable[[], Awaitable[T]]) -> T:
    """Repeat an idempotent call once if its connection was broken.

    redis-py discards a connection that fails, so the second call uses a new one.
    Timeouts are not repeated: the server may still be executing the first call.
    """
    try:
        return await call()
    except RedisConnectionError:
        return await call()


def _identifiers(admission: Admission) -> dict[str, object]:
    # Reservation IDs are random and never accepted from clients, so logging them
    # grants nothing.
    return {"principal_id": admission.principal_id, "reservation_id": admission.reservation_id}
