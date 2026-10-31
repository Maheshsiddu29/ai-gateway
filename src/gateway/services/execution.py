"""Provider attempts: retries and circuit breaking.

Every attempt runs within the request deadline, and backoff delays consume it too,
so retries never extend a request. A streamed attempt counts as successful once its
first event arrives; after that, a failure ends the stream and is never retried,
because the client may already have received output.
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing

from gateway.admission.usage import Attempt, UsageLedger
from gateway.core.config import ResilienceConfig
from gateway.core.metrics import Metrics
from gateway.errors import GatewayError, ProviderError, ProviderUnavailableError
from gateway.providers.base import Completion, Provider, ProviderRequest, StreamEnd, StreamEvent
from gateway.resilience.breaker import AttemptOutcome, BreakerState, CircuitBreaker, outcome_of
from gateway.resilience.deadline import Deadline
from gateway.resilience.retry import RetryPolicy

logger = logging.getLogger(__name__)

type _OpenStream = tuple[AsyncGenerator[StreamEvent], StreamEvent]


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
        self, provider: str, request: ProviderRequest, deadline: Deadline, ledger: UsageLedger
    ) -> Completion:
        completion, attempt = await self._attempts(
            provider, deadline, ledger, lambda adapter: adapter.complete(request)
        )
        attempt.succeeded(completion.usage)
        return completion

    async def stream(
        self, provider: str, request: ProviderRequest, deadline: Deadline, ledger: UsageLedger
    ) -> AsyncGenerator[StreamEvent]:
        (events, first), attempt = await self._attempts(
            provider, deadline, ledger, lambda adapter: _open_stream(adapter, request)
        )
        async with aclosing(events):
            event: StreamEvent | None = first
            while event is not None:
                if isinstance(event, StreamEnd):
                    attempt.succeeded(event.usage)
                yield event
                try:
                    async with deadline.limit():
                        event = await anext(events, None)
                except BaseException as exc:
                    attempt.failed(exc)
                    if outcome_of(exc) is AttemptOutcome.FAILURE:
                        self._breakers[provider].record_failure()
                    raise

    async def _attempts[T](
        self,
        name: str,
        deadline: Deadline,
        ledger: UsageLedger,
        run: Callable[[Provider], Awaitable[T]],
    ) -> tuple[T, Attempt]:
        provider = self._providers[name]
        breaker = self._breakers[name]
        number = 0
        while True:
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
            started = time.perf_counter()
            try:
                async with deadline.limit():
                    result = await run(provider)
            except BaseException as exc:
                attempt.failed(exc)
                breaker.release(permit, outcome_of(exc))
                self._observe(name, started, _outcome_label(exc))
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
                await self._sleep(delay)
                continue
            breaker.release(permit, AttemptOutcome.SUCCESS)
            self._observe(name, started, "success")
            return result, attempt

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
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return "internal_error"
