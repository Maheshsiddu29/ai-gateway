import httpx
import pytest

from gateway.errors import (
    ErrorType,
    ProviderRateLimitError,
)
from gateway.providers.http import error_from_status, error_from_transport


@pytest.mark.parametrize(
    ("status", "error_type", "client_status"),
    [
        (400, ErrorType.INVALID_REQUEST, 400),
        (422, ErrorType.INVALID_REQUEST, 400),
        (401, ErrorType.PROVIDER_CONFIGURATION, 502),
        (403, ErrorType.PROVIDER_CONFIGURATION, 502),
        (404, ErrorType.PROVIDER_CONFIGURATION, 502),
        (408, ErrorType.PROVIDER_TIMEOUT, 504),
        (429, ErrorType.PROVIDER_RATE_LIMITED, 429),
        (500, ErrorType.PROVIDER_UNAVAILABLE, 502),
        (503, ErrorType.PROVIDER_UNAVAILABLE, 502),
        (504, ErrorType.PROVIDER_TIMEOUT, 504),
        (529, ErrorType.PROVIDER_UNAVAILABLE, 502),
        (307, ErrorType.PROVIDER_UNAVAILABLE, 502),
    ],
)
def test_status_mapping(status: int, error_type: ErrorType, client_status: int) -> None:
    error = error_from_status(httpx.Response(status))

    assert error.error_type is error_type
    assert error.status_code == client_status
    assert error.upstream_status == status


def test_keeps_upstream_error_type_but_not_message() -> None:
    response = httpx.Response(
        400, json={"error": {"type": "invalid_request_error", "message": "echo: secret prompt"}}
    )

    error = error_from_status(response)

    assert error.upstream_error_type == "invalid_request_error"
    assert "secret prompt" not in error.message


def test_reads_top_level_error_type() -> None:
    response = httpx.Response(400, json={"object": "error", "type": "BadRequestError"})

    assert error_from_status(response).upstream_error_type == "BadRequestError"


@pytest.mark.parametrize(
    ("header", "expected"), [("12", 12), ("0", 0), ("999999", 86_400), ("soon", None), ("", None)]
)
def test_retry_after(header: str, expected: int | None) -> None:
    error = error_from_status(httpx.Response(429, headers={"retry-after": header}))

    assert isinstance(error, ProviderRateLimitError)
    assert error.retry_after_seconds == expected


def test_retry_after_http_date_is_not_forwarded() -> None:
    response = httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})

    error = error_from_status(response)

    assert isinstance(error, ProviderRateLimitError)
    assert error.retry_after_seconds is None


@pytest.mark.parametrize(
    ("exc", "error_type", "code"),
    [
        (
            httpx.ConnectTimeout("httpx-detail"),
            ErrorType.PROVIDER_TIMEOUT,
            "provider_connect_timeout",
        ),
        (httpx.ReadTimeout("httpx-detail"), ErrorType.PROVIDER_TIMEOUT, "provider_read_timeout"),
        (httpx.PoolTimeout("httpx-detail"), ErrorType.PROVIDER_TIMEOUT, "provider_pool_timeout"),
        (
            httpx.ConnectError("refused"),
            ErrorType.PROVIDER_UNAVAILABLE,
            "provider_connection_error",
        ),
        (
            httpx.RemoteProtocolError("peer closed"),
            ErrorType.PROVIDER_UNAVAILABLE,
            "provider_connection_error",
        ),
    ],
)
def test_transport_error_mapping(
    exc: httpx.TransportError, error_type: ErrorType, code: str
) -> None:
    error = error_from_transport(exc)

    assert (error.error_type, error.code) == (error_type, code)
    assert str(exc) not in error.message
