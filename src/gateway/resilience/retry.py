"""Which provider failures are retried, and after how long.

Inference calls are not idempotent: a provider that received a request may run it
and charge for it even if the gateway never sees the response. A failure is
therefore retried only when the provider certainly did not run the request (the
connection was never established, or it explicitly rejected the request), the
failure is transient (rate limiting or connect timeouts), and its
status, if any, is in the configured allow-list.
"""

from collections.abc import Callable
from dataclasses import dataclass

from gateway.core.config import ResilienceConfig
from gateway.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_TRANSIENT = (ProviderRateLimitError, ProviderUnavailableError, ProviderTimeoutError)
# A configuration error (bad credentials, unknown model) at one provider says nothing
# about another, so it is worth falling back from, though not retrying.
_FALLBACK = (*_TRANSIENT, ProviderConfigurationError)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_backoff_seconds: float
    max_backoff_seconds: float
    jitter_ratio: float
    max_retry_after_seconds: float
    retry_on_status: frozenset[int]

    @classmethod
    def from_config(cls, config: ResilienceConfig) -> "RetryPolicy":
        return cls(
            max_attempts=config.max_attempts_per_provider,
            initial_backoff_seconds=config.retry_initial_backoff_seconds,
            max_backoff_seconds=config.retry_max_backoff_seconds,
            jitter_ratio=config.retry_jitter_ratio,
            max_retry_after_seconds=config.max_retry_after_seconds,
            retry_on_status=config.retry_on_status,
        )

    def is_retryable(self, exc: ProviderError) -> bool:
        if exc.may_have_processed or not isinstance(exc, _TRANSIENT):
            return False
        return exc.upstream_status is None or exc.upstream_status in self.retry_on_status

    def allows_fallback(self, exc: ProviderError) -> bool:
        """Whether a failure, after any retries, may be followed by the next provider."""
        return not exc.may_have_processed and isinstance(exc, _FALLBACK)

    def backoff(self, attempt: int, random: Callable[[], float]) -> float:
        """Delay after the given (1-based) attempt: exponential, capped, with jitter."""
        delay = min(self.max_backoff_seconds, self.initial_backoff_seconds * 2.0 ** (attempt - 1))
        return delay * (1 - self.jitter_ratio * random())

    def delay_before_retry(
        self,
        exc: ProviderError,
        attempt: int,
        remaining_seconds: float,
        random: Callable[[], float],
    ) -> float | None:
        """The delay before another attempt, or None if the failure must not be retried."""
        if attempt >= self.max_attempts or not self.is_retryable(exc):
            return None
        delay = self.backoff(attempt, random)
        if exc.retry_after_seconds is not None:
            if exc.retry_after_seconds > self.max_retry_after_seconds:
                return None
            delay = max(delay, exc.retry_after_seconds)
        # Waiting past the deadline would leave no time for the retry itself.
        if delay >= remaining_seconds:
            return None
        return delay
