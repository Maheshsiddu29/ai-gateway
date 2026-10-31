import httpx
import pytest

from gateway.core.config import ResilienceConfig
from gateway.errors import ProviderError, ProviderRateLimitError
from gateway.providers.http import error_from_status, error_from_transport, malformed_response_error
from gateway.resilience.retry import RetryPolicy

POLICY = RetryPolicy.from_config(
    ResilienceConfig(
        max_attempts_per_provider=3,
        retry_initial_backoff_seconds=0.1,
        retry_max_backoff_seconds=0.3,
        retry_jitter_ratio=0.5,
        max_retry_after_seconds=5,
    )
)


def no_jitter() -> float:
    return 0.0


def full_jitter() -> float:
    return 1.0


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (error_from_status(httpx.Response(429)), True),
        (error_from_status(httpx.Response(500)), False),
        (error_from_status(httpx.Response(503)), False),
        (error_from_status(httpx.Response(502)), False),
        (error_from_status(httpx.Response(529)), False),
        (error_from_transport(httpx.ConnectError("refused")), True),
        (error_from_transport(httpx.ConnectTimeout("slow")), True),
        (error_from_transport(httpx.PoolTimeout("busy")), True),
        # The provider may have run these.
        (error_from_status(httpx.Response(504)), False),
        (error_from_transport(httpx.ReadTimeout("slow")), False),
        (error_from_transport(httpx.RemoteProtocolError("dropped")), False),
        (malformed_response_error(), False),
        # Not transient.
        (error_from_status(httpx.Response(501)), False),
        (error_from_status(httpx.Response(400)), False),
        (error_from_status(httpx.Response(401)), False),
        (error_from_status(httpx.Response(404)), False),
    ],
)
def test_retry_eligibility(exc: ProviderError, retryable: bool) -> None:
    assert POLICY.is_retryable(exc) is retryable


def test_backoff_doubles_up_to_the_cap() -> None:
    delays = [POLICY.backoff(attempt, no_jitter) for attempt in (1, 2, 3, 4)]

    assert delays == pytest.approx([0.1, 0.2, 0.3, 0.3])


def test_jitter_only_shortens_the_delay_within_its_ratio() -> None:
    assert POLICY.backoff(2, full_jitter) == pytest.approx(0.1)
    assert POLICY.backoff(2, lambda: 0.5) == pytest.approx(0.15)


def test_stops_after_the_maximum_attempts() -> None:
    exc = error_from_status(httpx.Response(429))

    assert POLICY.delay_before_retry(exc, 2, 60, no_jitter) is not None
    assert POLICY.delay_before_retry(exc, 3, 60, no_jitter) is None


def test_waits_at_least_the_providers_retry_after() -> None:
    exc = ProviderRateLimitError("limited", retry_after_seconds=2, may_have_processed=False)

    assert POLICY.delay_before_retry(exc, 1, 60, no_jitter) == 2


def test_does_not_retry_when_retry_after_exceeds_the_cap() -> None:
    exc = ProviderRateLimitError("limited", retry_after_seconds=6, may_have_processed=False)

    assert POLICY.delay_before_retry(exc, 1, 60, no_jitter) is None


def test_does_not_retry_when_the_delay_would_use_up_the_deadline() -> None:
    exc = error_from_status(httpx.Response(429))

    assert POLICY.delay_before_retry(exc, 1, remaining_seconds=0.1, random=no_jitter) is None
    assert POLICY.delay_before_retry(exc, 1, remaining_seconds=0.11, random=no_jitter) == 0.1


def test_status_allow_list_is_respected() -> None:
    policy = RetryPolicy.from_config(ResilienceConfig(retry_on_status=frozenset({429})))

    assert policy.is_retryable(error_from_status(httpx.Response(429)))
    assert not policy.is_retryable(error_from_status(httpx.Response(503)))


@pytest.mark.parametrize("status", [400, 404, 418])
def test_config_rejects_non_transient_statuses(status: int) -> None:
    with pytest.raises(ValueError, match="only 429 and 5xx"):
        ResilienceConfig(retry_on_status=frozenset({status}))


@pytest.mark.parametrize(
    ("exc", "allowed"),
    [
        (error_from_status(httpx.Response(503)), False),
        (error_from_status(httpx.Response(429)), True),
        (error_from_status(httpx.Response(401)), True),
        (error_from_status(httpx.Response(404)), True),
        (error_from_transport(httpx.ConnectError("refused")), True),
        (error_from_status(httpx.Response(400)), False),
        (error_from_status(httpx.Response(504)), False),
        (error_from_transport(httpx.ReadTimeout("slow")), False),
        (malformed_response_error(), False),
    ],
)
def test_fallback_eligibility(exc: ProviderError, allowed: bool) -> None:
    assert POLICY.allows_fallback(exc) is allowed
