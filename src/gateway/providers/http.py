"""HTTP plumbing shared by provider adapters: client construction and error mapping."""

import json
from collections.abc import Mapping

import httpx

from gateway import __version__
from gateway.core.config import ProviderConfig
from gateway.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRejectedRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

USER_AGENT = f"ai-gateway/{__version__}"
_MAX_RETRY_AFTER_SECONDS = 86_400
_TIMEOUT_STATUSES = (408, 504)
_REJECTED_STATUSES = (400, 401, 402, 403, 404, 413, 422, 429)
_NOT_SENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
_TIMEOUT_CODES: dict[type[httpx.TimeoutException], str] = {
    httpx.ConnectTimeout: "provider_connect_timeout",
    httpx.ReadTimeout: "provider_read_timeout",
    httpx.WriteTimeout: "provider_write_timeout",
    httpx.PoolTimeout: "provider_pool_timeout",
}


def create_client(
    config: ProviderConfig,
    *,
    headers: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create the client used for every request to one provider.

    Upstream requests are built only from configuration: the base URL is fixed per
    provider and redirects are not followed, so callers cannot direct the gateway
    to other hosts. Proxy and credential settings from the environment are
    ignored for the same reason.
    """
    return httpx.AsyncClient(
        base_url=str(config.base_url),
        headers={"user-agent": USER_AGENT, **headers},
        timeout=httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.write_timeout_seconds,
            pool=config.pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=config.max_connections,
            max_keepalive_connections=config.max_connections,
        ),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def error_from_transport(exc: httpx.TransportError) -> ProviderError:
    # Only failures before the request was sent prove the provider did not run it.
    # A read timeout or a dropped connection may follow a request the provider
    # received and is still processing.
    not_sent = isinstance(exc, _NOT_SENT_ERRORS)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(
            "The provider did not respond in time.",
            code=_TIMEOUT_CODES.get(type(exc), "provider_timeout"),
            may_have_processed=not not_sent,
        )
    return ProviderUnavailableError(
        "Could not communicate with the provider.",
        code="provider_connection_error",
        may_have_processed=not not_sent,
    )


def error_from_status(response: httpx.Response) -> ProviderError:
    """Map an upstream error status to a gateway error.

    The response must already be read. Only the upstream error ``type`` is kept,
    for logging; upstream messages are never forwarded because they can echo
    request content.
    """
    status = response.status_code
    error_type = _upstream_error_type(response)
    # A 5xx can follow inference execution or come from an intermediary. Only
    # explicit client/account rejections imply no generation was accepted.
    processed = status not in _REJECTED_STATUSES

    if status in (400, 413, 422):
        return ProviderRejectedRequestError(
            "The provider rejected the request.",
            upstream_status=status,
            upstream_error_type=error_type,
            may_have_processed=processed,
        )
    if status in (401, 402, 403):
        return ProviderConfigurationError(
            "The provider rejected the gateway's credentials or account.",
            code="provider_authentication_failed",
            upstream_status=status,
            upstream_error_type=error_type,
            may_have_processed=processed,
        )
    if status == 404:
        return ProviderConfigurationError(
            "The provider does not recognize the configured endpoint or model.",
            code="provider_not_found",
            upstream_status=status,
            upstream_error_type=error_type,
            may_have_processed=processed,
        )
    if status == 429:
        return ProviderRateLimitError(
            "The provider's rate limit was exceeded.",
            retry_after_seconds=_retry_after_seconds(response),
            upstream_status=status,
            upstream_error_type=error_type,
            may_have_processed=processed,
        )
    if status in _TIMEOUT_STATUSES:
        return ProviderTimeoutError(
            "The provider timed out.",
            upstream_status=status,
            upstream_error_type=error_type,
            may_have_processed=processed,
        )
    return ProviderUnavailableError(
        "The provider is unavailable."
        if status >= 500
        else "The provider returned an unexpected response.",
        code="provider_server_error" if status >= 500 else "unexpected_provider_status",
        upstream_status=status,
        upstream_error_type=error_type,
        may_have_processed=processed,
    )


def malformed_response_error() -> ProviderUnavailableError:
    return ProviderUnavailableError(
        "The provider returned a malformed response.", code="malformed_provider_response"
    )


def incomplete_stream_error() -> ProviderUnavailableError:
    return ProviderUnavailableError(
        "The provider stream ended before the response was complete.",
        code="provider_stream_incomplete",
    )


def is_event_stream(response: httpx.Response) -> bool:
    content_type: str = response.headers.get("content-type", "")
    return content_type.partition(";")[0].strip().lower() == "text/event-stream"


def _retry_after_seconds(response: httpx.Response) -> int | None:
    # Only the delay-seconds form is forwarded; HTTP-date values are dropped
    # rather than converted, to avoid relying on clock agreement with the provider.
    value = response.headers.get("retry-after", "").strip()
    if not (value.isascii() and value.isdigit()) or len(value) > 10:
        return None
    return min(int(value), _MAX_RETRY_AFTER_SECONDS)


def _upstream_error_type(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    candidate = error.get("type") if isinstance(error, dict) else body.get("type")
    if isinstance(candidate, str):
        return candidate[:64]
    return None
