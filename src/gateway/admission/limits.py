from dataclasses import dataclass

from gateway.core.config import RateLimitsConfig


@dataclass(frozen=True)
class TokenBucket:
    """Refills continuously at ``rate_per_minute`` up to ``capacity``."""

    rate_per_minute: int
    capacity: int


@dataclass(frozen=True)
class KeyLimits:
    requests: TokenBucket | None
    tokens: TokenBucket | None
    max_concurrent: int | None

    @classmethod
    def from_config(cls, config: RateLimitsConfig) -> "KeyLimits":
        requests = None
        if config.requests_per_minute is not None:
            requests = TokenBucket(
                config.requests_per_minute, config.request_burst or config.requests_per_minute
            )
        tokens = None
        if config.tokens_per_minute is not None:
            tokens = TokenBucket(
                config.tokens_per_minute, config.token_burst or config.tokens_per_minute
            )
        return cls(requests=requests, tokens=tokens, max_concurrent=config.max_concurrent_requests)
