from gateway.admission.limits import KeyLimits, TokenBucket
from gateway.core.config import RateLimitsConfig


def test_burst_defaults_to_the_per_minute_rate() -> None:
    limits = KeyLimits.from_config(
        RateLimitsConfig(requests_per_minute=60, tokens_per_minute=1000, token_burst=5000)
    )

    assert limits.requests == TokenBucket(rate_per_minute=60, capacity=60)
    assert limits.tokens == TokenBucket(rate_per_minute=1000, capacity=5000)
    assert limits.max_concurrent is None


def test_no_configured_limits_means_no_buckets() -> None:
    limits = KeyLimits.from_config(RateLimitsConfig(max_concurrent_requests=3))

    assert limits == KeyLimits(requests=None, tokens=None, max_concurrent=3)
