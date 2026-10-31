"""Provider attempts: retries, circuit breaking, and fallback.

A request has one or more candidates: the alias's primary provider, then its
configured fallbacks. Each candidate is tried with retries; if it still fails, and
the failure shows the provider did not run the request, the next candidate is
tried. Failures that may have run (timeouts after sending, interrupted streams) end
the request instead.

Every attempt runs within the request deadline, and backoff delays consume it too,
so neither retries nor fallback extend a request. A streamed attempt counts as
successful once its first event arrives; after that, a failure ends the stream and
is never retried or redirected, because the client may already have output.
"""

import asyncio
import itertools
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass

from gateway.admission.usage import Attempt, UsageLedger
from gateway.core.config import ResilienceConfig
from gateway.core.metrics import Metrics
from gateway.cost.session import CostSession
from gateway.errors import (
    GatewayError,
    InternalGatewayError,
    ProviderError,
    ProviderUnavailableError,
)
from gateway.providers.base import Completion, Provider, ProviderRequest, StreamEnd, StreamEvent
from gateway.resilience.breaker import (
    AttemptOutcome,
    BreakerState,
    CircuitBreaker,
    Permit,
    outcome_of,
)
from gateway.resilience.deadline import Deadline
from gateway.resilience.retry import RetryPolicy

logger = logging.getLogger(__name__)

type _OpenStream = tuple[AsyncGenerator[StreamEvent], StreamEvent]


@dataclass(frozen=True)
class Candidate:
    """A provider and the request to send it."""

    provider: str
    request: ProviderRequest


class ProviderExecutor:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        breakers: Mapping[str, CircuitBreaker],
        policy: RetryPolicy,
        metrics: Metrics,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random.random,
    ) -> None:
        self._providers = providers
        self._breakers = breakers
        self._policy = policy
        self._metrics = metrics
        self._sleep = sleep
        self._random = random

    async def complete(
        self,
        candidates: Sequence[Candidate],
        deadline: Deadline,
        ledger: UsageLedger,
        cost: CostSession | None = None,
    ) -> Completion:
        completion, attempt, candidate, permit = await self._first_success(
            candidates, deadline, ledger, lambda adapter, request: adapter.complete(request), cost
        )
        attempt.succeeded(completion.usage)
        self._breakers[candidate.provider].release(permit, AttemptOutcome.SUCCESS)
        self._observe(candidate.provider, attempt.started_at, "success")
        if cost is not None and attempt.cost_attempt_id is not None:
            await cost.settle(
                attempt.cost_attempt_id,
                completion.usage,
                may_have_processed=True,
                classification="success",
            )
        return completion

    async def stream(
        self,
        candidates: Sequence[Candidate],
        deadline: Deadline,
        ledger: UsageLedger,
        cost: CostSession | None = None,
    ) -> AsyncGenerator[StreamEvent]:
        (events, first), attempt, candidate, permit = await self._first_success(
            candidates, deadline, ledger, _open_stream, cost
        )
        outcome = AttemptOutcome.NEUTRAL
        label = "cancelled"
        ended = False
        try:
            async with aclosing(events):
                event: StreamEvent | None = first
                while event is not None:
                    if isinstance(event, StreamEnd):
                        attempt.succeeded(event.usage)
                        ended = True
                    yield event
                    async with deadline.limit():
                        event = await anext(events, None)
                if not ended:
                    raise InternalGatewayError("The provider stream ended unexpectedly.")
            outcome = AttemptOutcome.SUCCESS
            label = "success"
        except BaseException as exc:
            attempt.failed(exc)
            outcome = outcome_of(exc)
            label = _outcome_label(exc)
            raise
        finally:
            # Release the breaker even when financial settlement fails.
            self._breakers[candidate.provider].release(permit, outcome)
            self._observe(candidate.provider, attempt.started_at, label)
            if cost is not None and attempt.cost_attempt_id is not None:
                await cost.settle(
                    attempt.cost_attempt_id,
                    attempt.usage,
                    may_have_processed=attempt.may_have_processed,
                    classification=label,
                )

    async def _first_success[T](
        self,
        candidates: Sequence[Candidate],
        deadline: Deadline,
        ledger: UsageLedger,
        run: Callable[[Provider, ProviderRequest], Awaitable[T]],
        cost: CostSession | None,
    ) -> tuple[T, Attempt, Candidate, Permit]:
        for candidate, following in itertools.pairwise(candidates):
            try:
                result, attempt, permit = await self._attempts(
                    candidate, deadline, ledger, run, cost
                )
            except ProviderError as exc:
                if not self._policy.allows_fallback(exc):
                    raise
                self._metrics.provider_fallbacks.labels(
                    candidate.provider, following.provider
                ).inc()
                logger.warning(
                    "falling back to another provider",
                    extra={
                        "provider": candidate.provider,
                        "fallback_provider": following.provider,
                        "error_code": exc.code,
                    },
                )
                continue
            return result, attempt, candidate, permit
        last = candidates[-1]
        result, attempt, permit = await self._attempts(last, deadline, ledger, run, cost)
        return result, attempt, last, permit

    async def _attempts[T](
        self,
        candidate: Candidate,
        deadline: Deadline,
        ledger: UsageLedger,
        run: Callable[[Provider, ProviderRequest], Awaitable[T]],
        cost: CostSession | None,
    ) -> tuple[T, Attempt, Permit]:
        name = candidate.provider
        provider = self._providers[name]
        breaker = self._breakers[name]
        number = 0
        while True:
            deadline.check()
            number += 1
            permit = breaker.try_acquire()
            if permit is None:
                self._metrics.provider_attempts.labels(name, "circuit_open").inc()
                raise ProviderUnavailableError(
                    "The provider is temporarily unavailable.",
                    code="provider_circuit_open",
                    may_have_processed=False,
                )
            attempt = ledger.start(name)
            started = attempt.started_at
            dispatched = False
            try:
                if cost is not None:
                    attempt.cost_attempt_id = await cost.reserve(name, candidate.request)
                    await cost.store.dispatch(attempt.cost_attempt_id)
                    dispatched = True
                async with deadline.limit():
                    result = await run(provider, candidate.request)
            except BaseException as exc:
                attempt.failed(exc)
                if not dispatched:
                    attempt.may_have_processed = False
                breaker.release(permit, outcome_of(exc))
                self._observe(name, started, _outcome_label(exc))
                if cost is not None and attempt.cost_attempt_id is not None:
                    await cost.settle(
                        attempt.cost_attempt_id,
                        None,
                        may_have_processed=dispatched and attempt.may_have_processed,
                        classification=_outcome_label(exc),
                    )
                delay = self._retry_delay(exc, number, deadline)
                if delay is None:
                    raise
                self._metrics.provider_retries.labels(name).inc()
                logger.info(
                    "retrying provider attempt",
                    extra={
                        "provider": name,
                        "attempt": number,
                        "error_code": getattr(exc, "code", None),
                        "delay_ms": round(delay * 1000, 1),
                    },
                )
                async with deadline.limit():
                    await self._sleep(delay)
                continue
            return result, attempt, permit

    def _retry_delay(self, exc: BaseException, attempt: int, deadline: Deadline) -> float | None:
        if not isinstance(exc, ProviderError):
            return None
        return self._policy.delay_before_retry(exc, attempt, deadline.remaining(), self._random)

    def _observe(self, provider: str, started: float, outcome: str) -> None:
        self._metrics.provider_attempts.labels(provider, outcome).inc()
        self._metrics.provider_attempt_duration.labels(provider).observe(
            time.perf_counter() - started
        )


def build_executor(
    providers: Mapping[str, Provider],
    config: ResilienceConfig,
    metrics: Metrics,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ProviderExecutor:
    def record_transition(provider: str, state: BreakerState) -> None:
        metrics.circuit_transitions.labels(provider, state.value).inc()
        logger.warning(
            "circuit breaker state changed", extra={"provider": provider, "state": state.value}
        )

    breakers = {
        name: CircuitBreaker(
            name, config.circuit_breaker, clock=clock, on_transition=record_transition
        )
        for name in providers
    }
    return ProviderExecutor(providers, breakers, RetryPolicy.from_config(config), metrics)


async def _open_stream(provider: Provider, request: ProviderRequest) -> _OpenStream:
    # Reading the first event is part of the attempt: errors reported before any
    # output can still be retried, and the client has seen nothing yet.
    events = provider.stream(request)
    try:
        first = await anext(events)
    except BaseException:
        await events.aclose()
        raise
    return events, first


def _outcome_label(exc: BaseException) -> str:
    if isinstance(exc, GatewayError):
        return exc.error_type.value
    if isinstance(exc, asyncio.CancelledError | GeneratorExit):
        return "cancelled"
    return "internal_error"
