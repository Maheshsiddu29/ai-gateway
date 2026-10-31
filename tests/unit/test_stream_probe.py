import asyncio
from collections.abc import AsyncGenerator

import pytest
from prometheus_client import generate_latest

from gateway.admission.usage import UsageLedger
from gateway.core.config import CircuitBreakerConfig, ResilienceConfig
from gateway.core.metrics import Metrics
from gateway.errors import ProviderUnavailableError
from gateway.providers.base import ProviderRequest, StreamEnd, StreamEvent, TextDelta
from gateway.resilience.breaker import BreakerState, CircuitBreaker
from gateway.resilience.deadline import Deadline
from gateway.resilience.retry import RetryPolicy
from gateway.schemas.completions import ChatMessage
from gateway.services.execution import Candidate, ProviderExecutor
from tests.fakes import FakeProvider


class ProbeProvider(FakeProvider):
    def __init__(self, *, fail: bool) -> None:
        super().__init__()
        self.fail = fail
        self.stream_closed = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        try:
            yield TextDelta("partial")
            if self.fail:
                raise ProviderUnavailableError("interrupted")
            yield StreamEnd("stop", None)
        finally:
            self.stream_closed.set()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_probe_remains_held_until_the_whole_stream_finishes(outcome: str) -> None:
    clock = [100.0]
    transitions: list[BreakerState] = []
    config = CircuitBreakerConfig(failure_threshold=1, recovery_seconds=1)
    breaker = CircuitBreaker(
        "fake",
        config,
        clock=lambda: clock[0],
        on_transition=lambda _, state: transitions.append(state),
    )
    breaker.record_failure()
    clock[0] += 1
    provider = ProbeProvider(fail=outcome == "failure")
    metrics = Metrics()
    executor = ProviderExecutor(
        {"fake": provider},
        {"fake": breaker},
        RetryPolicy.from_config(ResilienceConfig(max_attempts_per_provider=1)),
        metrics,
    )
    request = ProviderRequest(
        model="m",
        messages=(ChatMessage(role="user", content="hi"),),
        max_tokens=10,
        temperature=None,
        top_p=None,
        stop=(),
        include_usage=False,
    )
    events = executor.stream([Candidate("fake", request)], Deadline(10), UsageLedger())
    assert await anext(events) == TextDelta("partial")
    assert list(transitions)[-1] is BreakerState.HALF_OPEN
    assert breaker.try_acquire() is None
    if outcome == "cancelled":
        await events.aclose()
        assert list(transitions)[-1] is BreakerState.HALF_OPEN
        assert breaker.try_acquire() is not None
    elif outcome == "failure":
        with pytest.raises(ProviderUnavailableError):
            await anext(events)
        assert list(transitions)[-1] is BreakerState.OPEN
    else:
        assert isinstance(await anext(events), StreamEnd)
        assert breaker.try_acquire() is None
        assert await anext(events, None) is None
        assert list(transitions)[-1] is BreakerState.CLOSED
    assert provider.stream_closed.is_set()

    label = {"success": "success", "failure": "provider_unavailable", "cancelled": "cancelled"}[
        outcome
    ]
    text = generate_latest(metrics.registry).decode()
    assert f'gateway_provider_attempts_total{{outcome="{label}",provider="fake"}} 1.0' in text
    if outcome != "success":
        assert 'gateway_provider_attempts_total{outcome="success"' not in text
