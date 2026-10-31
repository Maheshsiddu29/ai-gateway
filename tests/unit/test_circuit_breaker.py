from gateway.core.config import CircuitBreakerConfig
from gateway.resilience.breaker import AttemptOutcome, BreakerState, CircuitBreaker

CONFIG = CircuitBreakerConfig(
    failure_threshold=3, failure_window_seconds=10, recovery_seconds=5, half_open_max_probes=1
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(self, config: CircuitBreakerConfig = CONFIG) -> None:
        self.clock = FakeClock()
        self.transitions: list[BreakerState] = []
        self.breaker = CircuitBreaker(
            "p",
            config,
            clock=self.clock,
            on_transition=lambda _, state: self.transitions.append(state),
        )

    def state(self) -> BreakerState:
        # A method rather than the property, so type checkers do not narrow it
        # across the state changes a test makes.
        return self.breaker.state

    def fail(self, times: int = 1) -> None:
        for _ in range(times):
            permit = self.breaker.try_acquire()
            assert permit is not None
            self.breaker.release(permit, AttemptOutcome.FAILURE)

    def open(self) -> None:
        self.fail(CONFIG.failure_threshold)
        assert self.state() is BreakerState.OPEN


def test_opens_after_the_threshold_within_the_window() -> None:
    harness = Harness()

    harness.fail(2)
    assert harness.state() is BreakerState.CLOSED
    harness.fail()

    assert harness.state() is BreakerState.OPEN
    assert harness.breaker.try_acquire() is None
    assert harness.transitions == [BreakerState.OPEN]


def test_failures_outside_the_window_do_not_open_it() -> None:
    harness = Harness()

    for _ in range(5):
        harness.fail()
        harness.clock.now += 6

    assert harness.state() is BreakerState.CLOSED


def test_neutral_outcomes_and_successes_are_not_failures() -> None:
    harness = Harness()

    for outcome in [AttemptOutcome.NEUTRAL, AttemptOutcome.SUCCESS] * 5:
        permit = harness.breaker.try_acquire()
        assert permit is not None
        harness.breaker.release(permit, outcome)

    assert harness.state() is BreakerState.CLOSED


def test_becomes_half_open_after_recovery_and_limits_probes() -> None:
    harness = Harness()
    harness.open()

    harness.clock.now += 4.9
    assert harness.breaker.try_acquire() is None
    harness.clock.now += 0.1
    probe = harness.breaker.try_acquire()

    assert probe is not None
    assert probe.probe
    assert harness.state() is BreakerState.HALF_OPEN
    assert harness.breaker.try_acquire() is None


def test_successful_probe_closes_and_resets_failures() -> None:
    harness = Harness()
    harness.open()
    harness.clock.now += 5
    probe = harness.breaker.try_acquire()
    assert probe is not None

    harness.breaker.release(probe, AttemptOutcome.SUCCESS)

    assert harness.state() is BreakerState.CLOSED
    harness.fail(2)
    assert harness.state() is BreakerState.CLOSED
    assert harness.transitions == [BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.CLOSED]


def test_failed_probe_reopens_for_another_recovery_period() -> None:
    harness = Harness()
    harness.open()
    harness.clock.now += 5
    probe = harness.breaker.try_acquire()
    assert probe is not None

    harness.breaker.release(probe, AttemptOutcome.FAILURE)

    assert harness.state() is BreakerState.OPEN
    harness.clock.now += 4
    assert harness.breaker.try_acquire() is None
    harness.clock.now += 1
    assert harness.breaker.try_acquire() is not None


def test_a_neutral_probe_frees_its_slot_without_deciding() -> None:
    harness = Harness()
    harness.open()
    harness.clock.now += 5
    probe = harness.breaker.try_acquire()
    assert probe is not None

    harness.breaker.release(probe, AttemptOutcome.NEUTRAL)

    assert harness.state() is BreakerState.HALF_OPEN
    assert harness.breaker.try_acquire() is not None


def test_failures_after_a_permit_are_counted() -> None:
    harness = Harness()

    for _ in range(3):
        harness.breaker.record_failure()

    assert harness.state() is BreakerState.OPEN


def test_disabled_breaker_never_opens() -> None:
    harness = Harness(CircuitBreakerConfig(enabled=False, failure_threshold=1))

    harness.fail(10)

    assert harness.state() is BreakerState.CLOSED
    assert harness.transitions == []


def test_stale_probe_cannot_close_a_later_half_open_generation() -> None:
    config = CONFIG.model_copy(update={"half_open_max_probes": 2})
    harness = Harness(config)
    harness.open()
    harness.clock.now += 5
    stale = harness.breaker.try_acquire()
    failing = harness.breaker.try_acquire()
    assert stale is not None
    assert failing is not None
    harness.breaker.release(failing, AttemptOutcome.FAILURE)
    harness.clock.now += 5
    current = harness.breaker.try_acquire()
    assert current is not None
    harness.breaker.release(stale, AttemptOutcome.SUCCESS)
    assert harness.state() is BreakerState.HALF_OPEN
    harness.breaker.release(current, AttemptOutcome.SUCCESS)
    assert harness.state() is BreakerState.CLOSED


def test_duplicate_probe_release_does_not_free_another_slot() -> None:
    harness = Harness()
    harness.open()
    harness.clock.now += 5
    probe = harness.breaker.try_acquire()
    assert probe is not None
    harness.breaker.release(probe, AttemptOutcome.NEUTRAL)
    current = harness.breaker.try_acquire()
    assert current is not None
    harness.breaker.release(probe, AttemptOutcome.NEUTRAL)
    assert harness.breaker.try_acquire() is None


def test_available_predicts_try_acquire_without_changing_state() -> None:
    harness = Harness()
    assert harness.breaker.available()

    harness.open()
    assert not harness.breaker.available()

    harness.clock.now += CONFIG.recovery_seconds
    assert harness.breaker.available()
    # Only try_acquire moves the breaker to half-open.
    assert harness.state() is BreakerState.OPEN
    probe = harness.breaker.try_acquire()
    assert probe is not None
    assert harness.state() is BreakerState.HALF_OPEN
    # The single probe slot is taken.
    assert not harness.breaker.available()
    assert harness.breaker.try_acquire() is None
