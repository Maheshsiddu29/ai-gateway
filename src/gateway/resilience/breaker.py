"""A per-provider circuit breaker.

State is kept in process memory. Each gateway instance has its own breakers and
decides independently; breaker state is not shared between replicas.

Closed: requests pass. Provider failures (unavailability and timeouts, not client
errors, rate limiting, or cancellations) are counted; ``failure_threshold`` of them
within ``failure_window_seconds`` open the breaker.

Open: requests are refused without contacting the provider. After
``recovery_seconds`` the breaker becomes half-open.

Half-open: up to ``half_open_max_probes`` requests pass at once. A successful probe
closes the breaker; a failed probe reopens it. Further requests are refused while
the probes run.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from gateway.core.config import CircuitBreakerConfig
from gateway.errors import ProviderTimeoutError, ProviderUnavailableError


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    # Says nothing about provider health: client errors, cancellations, deadlines.
    NEUTRAL = "neutral"


@dataclass
class Permit:
    probe: bool
    generation: int = 0
    active: bool = True


def outcome_of(exc: BaseException) -> AttemptOutcome:
    if isinstance(exc, ProviderUnavailableError | ProviderTimeoutError):
        return AttemptOutcome.FAILURE
    return AttemptOutcome.NEUTRAL


class CircuitBreaker:
    def __init__(
        self,
        provider: str,
        config: CircuitBreakerConfig,
        *,
        clock: Callable[[], float],
        on_transition: Callable[[str, BreakerState], None],
    ) -> None:
        self._provider = provider
        self._config = config
        self._clock = clock
        self._on_transition = on_transition
        self._state = BreakerState.CLOSED
        self._failures: deque[float] = deque(maxlen=config.failure_threshold)
        self._opened_at = 0.0
        self._probes_in_flight = 0
        self._generation = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    def try_acquire(self) -> Permit | None:
        if not self._config.enabled or self._state is BreakerState.CLOSED:
            return Permit(probe=False, generation=self._generation)
        if (
            self._state is BreakerState.OPEN
            and self._clock() - self._opened_at >= self._config.recovery_seconds
        ):
            self._transition(BreakerState.HALF_OPEN)
        if (
            self._state is BreakerState.HALF_OPEN
            and self._probes_in_flight < self._config.half_open_max_probes
        ):
            self._probes_in_flight += 1
            return Permit(probe=True, generation=self._generation)
        return None

    def release(self, permit: Permit, outcome: AttemptOutcome) -> None:
        if not permit.active:
            return
        permit.active = False
        if not self._config.enabled or permit.generation != self._generation:
            return
        if not permit.probe:
            if outcome is AttemptOutcome.FAILURE:
                self.record_failure()
            return
        self._probes_in_flight -= 1
        # A probe's result only matters if the breaker is still half-open; another
        # probe may already have decided.
        if self._state is not BreakerState.HALF_OPEN:
            return
        if outcome is AttemptOutcome.SUCCESS:
            self._failures.clear()
            self._transition(BreakerState.CLOSED)
        elif outcome is AttemptOutcome.FAILURE:
            self._open()

    def record_failure(self) -> None:
        """Count a failure that happened after the attempt's permit was released,
        such as a stream that broke after its first event."""
        if not self._config.enabled or self._state is not BreakerState.CLOSED:
            return
        now = self._clock()
        self._failures.append(now)
        window_start = now - self._config.failure_window_seconds
        if len(self._failures) == self._failures.maxlen and self._failures[0] >= window_start:
            self._open()

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._transition(BreakerState.OPEN)

    def _transition(self, state: BreakerState) -> None:
        if state is not self._state:
            self._state = state
            self._generation += 1
            self._probes_in_flight = 0
            self._on_transition(self._provider, state)
