"""Gateway error categories shared by the API, service, and provider layers.

Every error raised towards a client is a ``GatewayError``. The ``error_type`` is a
stable category clients can branch on; ``code`` is a finer-grained, equally stable
identifier. Messages are written for clients and must never contain upstream
response bodies, credentials, or request content.
"""

from enum import StrEnum
from typing import ClassVar


class ErrorType(StrEnum):
    AUTHENTICATION = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PROVIDER_CONFIGURATION = "provider_configuration_error"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    ADMISSION_UNAVAILABLE = "admission_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL = "internal_error"


class GatewayError(Exception):
    error_type: ClassVar[ErrorType] = ErrorType.INTERNAL
    default_status_code: ClassVar[int] = 500
    default_code: ClassVar[str] = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.param = param
        self.status_code = status_code or self.default_status_code
        # Sent to the client as Retry-After when set.
        self.retry_after_seconds = retry_after_seconds


class AuthenticationError(GatewayError):
    error_type = ErrorType.AUTHENTICATION
    default_status_code = 401
    default_code = "invalid_api_key"


class InvalidRequestError(GatewayError):
    error_type = ErrorType.INVALID_REQUEST
    default_status_code = 400
    default_code = "invalid_request"


class UnsupportedCapabilityError(GatewayError):
    error_type = ErrorType.UNSUPPORTED_CAPABILITY
    default_status_code = 400
    default_code = "unsupported_capability"


class InternalGatewayError(GatewayError):
    pass


class RateLimitedError(GatewayError):
    """A gateway admission limit (requests, tokens, or concurrency) was reached."""

    error_type = ErrorType.RATE_LIMITED
    default_status_code = 429
    default_code = "rate_limited"


class AdmissionUnavailableError(GatewayError):
    """No trustworthy admission decision could be made, so the request was refused."""

    error_type = ErrorType.ADMISSION_UNAVAILABLE
    default_status_code = 503
    default_code = "admission_unavailable"


class DeadlineExceededError(GatewayError):
    """The request's end-to-end deadline expired before it completed."""

    error_type = ErrorType.DEADLINE_EXCEEDED
    default_status_code = 504
    default_code = "request_deadline_exceeded"


class ProviderError(GatewayError):
    """An upstream failure.

    ``upstream_status`` and ``upstream_error_type`` are recorded for logs only and
    are never included in client responses.

    ``may_have_processed`` is False only when the provider certainly did not run the
    request: the connection was never established, or the provider answered with an
    error status. It decides whether the attempt is charged against token limits and
    whether it may be retried. It defaults to True, the safe assumption.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        upstream_status: int | None = None,
        upstream_error_type: str | None = None,
        retry_after_seconds: int | None = None,
        may_have_processed: bool = True,
    ) -> None:
        super().__init__(message, code=code, retry_after_seconds=retry_after_seconds)
        self.upstream_status = upstream_status
        self.upstream_error_type = upstream_error_type
        self.may_have_processed = may_have_processed


class ProviderRejectedRequestError(ProviderError):
    """The provider refused a request the gateway considered valid."""

    error_type = ErrorType.INVALID_REQUEST
    default_status_code = 400
    default_code = "provider_rejected_request"


class ProviderConfigurationError(ProviderError):
    error_type = ErrorType.PROVIDER_CONFIGURATION
    default_status_code = 502
    default_code = "provider_configuration_error"


class ProviderRateLimitError(ProviderError):
    error_type = ErrorType.PROVIDER_RATE_LIMITED
    default_status_code = 429
    default_code = "provider_rate_limited"


class ProviderTimeoutError(ProviderError):
    error_type = ErrorType.PROVIDER_TIMEOUT
    default_status_code = 504
    default_code = "provider_timeout"


class ProviderUnavailableError(ProviderError):
    error_type = ErrorType.PROVIDER_UNAVAILABLE
    default_status_code = 502
    default_code = "provider_unavailable"


class ConfigurationError(Exception):
    """Invalid gateway configuration detected at startup. Never sent to clients."""
