"""Typed client for the gateway's ``POST /v1/chat/completions``.

The client sends only fields the gateway accepts and that every gateway adapter
can translate: ``model``, ``messages``, and ``max_tokens``. It never sends
sampling parameters, because some provider API versions reject them, and it never
streams.

A completion is returned only when the gateway answered 200 with a well-formed
body, ``finish_reason`` is ``stop``, and the content is not empty. Everything else
raises ``GatewayCallError`` with a stable code.

The client never retries. ``GatewayCallError.may_have_executed`` records whether
the gateway may have run, and charged for, the model call, so a future retry
policy can tell safe retries from duplicate executions.
"""

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agent_runtime import __version__

logger = logging.getLogger(__name__)

USER_AGENT = f"agent-runtime/{__version__}"
COMPLETIONS_PATH = "v1/chat/completions"
_MAX_RETRY_AFTER_SECONDS = 86_400
# The gateway accepts client request IDs with this syntax and reflects them.
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
# Gateway error types and codes are short identifiers; anything else is dropped.
_ERROR_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class GatewayErrorCode(StrEnum):
    REQUEST_TOO_LARGE = "gateway_request_too_large"
    AUTHENTICATION_FAILED = "gateway_authentication_failed"
    REQUEST_REJECTED = "gateway_request_rejected"
    RATE_LIMITED = "gateway_rate_limited"
    TIMEOUT = "gateway_timeout"
    UNAVAILABLE = "gateway_unavailable"
    UNEXPECTED_STATUS = "gateway_unexpected_status"
    RESPONSE_TOO_LARGE = "gateway_response_too_large"
    MALFORMED_RESPONSE = "gateway_malformed_response"
    COMPLETION_TRUNCATED = "completion_truncated"
    COMPLETION_REFUSED = "completion_refused"
    COMPLETION_EMPTY = "completion_empty"
    COMPLETION_UNSUPPORTED_STATE = "completion_unsupported_state"


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int


class GatewayCallError(Exception):
    """A gateway call that produced no usable completion.

    ``gateway_error_type`` and ``gateway_error_code`` come from the gateway's error
    envelope when it sent one. The gateway's message is not kept. ``usage`` is set
    when the gateway reported tokens for a completion that was then rejected here.
    """

    def __init__(
        self,
        code: GatewayErrorCode,
        message: str,
        *,
        may_have_executed: bool,
        status_code: int | None = None,
        gateway_error_type: str | None = None,
        gateway_error_code: str | None = None,
        retry_after_seconds: int | None = None,
        usage: TokenUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.may_have_executed = may_have_executed
        self.status_code = status_code
        self.gateway_error_type = gateway_error_type
        self.gateway_error_code = gateway_error_code
        self.retry_after_seconds = retry_after_seconds
        self.usage = usage


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class GatewayCompletion:
    content: str
    usage: TokenUsage | None
    gateway_request_id: str | None


@dataclass(frozen=True)
class GatewayTimeouts:
    connect: float = 5.0
    read: float = 120.0
    write: float = 30.0
    pool: float = 5.0


# Response models ignore unknown fields so that additive gateway changes do not
# break the runtime, but every field the runtime relies on is checked.
class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


class _Message(_ResponseModel):
    role: Literal["assistant"]
    content: str | None


class _Choice(_ResponseModel):
    index: Literal[0]
    message: _Message
    finish_reason: str | None


class _Usage(_ResponseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class _CompletionBody(_ResponseModel):
    object: Literal["chat.completion"]
    model: str
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: _Usage | None = None


class _ErrorDetail(_ResponseModel):
    type: str | None = None
    code: str | None = None


class _ErrorBody(_ResponseModel):
    error: _ErrorDetail


class GatewayClient:
    """One connection pool to one gateway, created at startup and closed at shutdown."""

    def __init__(
        self,
        base_url: str,
        api_key: SecretStr,
        *,
        timeouts: GatewayTimeouts,
        max_connections: int,
        max_request_bytes: int,
        max_response_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        # The URL comes only from configuration. Redirects are not followed and
        # proxy and .netrc settings from the environment are ignored, so the gateway
        # key is never sent anywhere else.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"user-agent": USER_AGENT},
            timeout=httpx.Timeout(
                connect=timeouts.connect,
                read=timeouts.read,
                write=timeouts.write,
                pool=timeouts.pool,
            ),
            limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_connections
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        request_id: str | None = None,
    ) -> GatewayCompletion:
        body = json.dumps(
            {
                "model": model,
                "messages": [message.model_dump() for message in messages],
                "max_tokens": max_tokens,
            },
            ensure_ascii=False,
        ).encode()
        if len(body) > self._max_request_bytes:
            raise GatewayCallError(
                GatewayErrorCode.REQUEST_TOO_LARGE,
                f"The request exceeds the {self._max_request_bytes}-byte limit.",
                may_have_executed=False,
            )
        headers = {
            "authorization": f"Bearer {self._api_key.get_secret_value()}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        if request_id is not None and _REQUEST_ID.fullmatch(request_id):
            headers["x-request-id"] = request_id

        started = time.perf_counter()
        status_code: int | None = None
        gateway_request_id: str | None = None
        # Stays "incomplete" if the call is cancelled, e.g. by the investigation deadline.
        outcome = "incomplete"
        try:
            async with self._client.stream(
                "POST", COMPLETIONS_PATH, content=body, headers=headers
            ) as response:
                status_code = response.status_code
                gateway_request_id = _safe_request_id(response.headers.get("x-request-id"))
                payload = await self._read_body(response)
            completion = _parse(response, payload, model, gateway_request_id)
            outcome = "ok"
        except GatewayCallError as exc:
            outcome = exc.code.value
            raise
        except httpx.RequestError as exc:
            error = _transport_error(exc)
            outcome = error.code.value
            raise error from None
        finally:
            logger.info(
                "gateway call finished",
                extra={
                    "model_alias": model,
                    "outcome": outcome,
                    "status_code": status_code,
                    "gateway_request_id": gateway_request_id,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        return completion

    async def _read_body(self, response: httpx.Response) -> bytes:
        limit = self._max_response_bytes
        too_large = GatewayCallError(
            GatewayErrorCode.RESPONSE_TOO_LARGE,
            f"The gateway response exceeds the {limit}-byte limit.",
            may_have_executed=_may_have_executed(response.status_code),
            status_code=response.status_code,
        )
        declared = response.headers.get("content-length")
        if (
            declared is not None
            and declared.isascii()
            and declared.isdigit()
            and int(declared) > limit
        ):
            raise too_large
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload += chunk
            if len(payload) > limit:
                raise too_large
        return bytes(payload)


def _parse(
    response: httpx.Response, payload: bytes, model: str, gateway_request_id: str | None
) -> GatewayCompletion:
    status = response.status_code
    if status != 200:
        raise _status_error(response, payload)
    malformed = GatewayCallError(
        GatewayErrorCode.MALFORMED_RESPONSE,
        "The gateway returned a malformed completion.",
        may_have_executed=True,
        status_code=status,
    )
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise malformed
    try:
        body = _CompletionBody.model_validate_json(payload)
    except ValidationError:
        raise malformed from None
    # The gateway echoes the requested alias; anything else is not this request.
    if body.model != model:
        raise malformed

    choice = body.choices[0]
    usage = (
        TokenUsage(body.usage.prompt_tokens, body.usage.completion_tokens)
        if body.usage is not None
        else None
    )
    content = choice.message.content
    match choice.finish_reason:
        case "stop":
            if content is None or not content.strip():
                raise GatewayCallError(
                    GatewayErrorCode.COMPLETION_EMPTY,
                    "The model returned an empty completion.",
                    may_have_executed=True,
                    status_code=status,
                    usage=usage,
                )
            return GatewayCompletion(content, usage, gateway_request_id)
        case "length":
            code, message = GatewayErrorCode.COMPLETION_TRUNCATED, "The completion was truncated."
        case "content_filter":
            code, message = GatewayErrorCode.COMPLETION_REFUSED, "The model refused the request."
        case _:
            code = GatewayErrorCode.COMPLETION_UNSUPPORTED_STATE
            message = "The completion ended for an unsupported reason."
    raise GatewayCallError(code, message, may_have_executed=True, status_code=status, usage=usage)


def _status_error(response: httpx.Response, payload: bytes) -> GatewayCallError:
    status = response.status_code
    error_type, error_code = _gateway_error(payload)
    may_have_executed = _may_have_executed(status)
    retry_after = None
    if status == 401:
        code, message = GatewayErrorCode.AUTHENTICATION_FAILED, "The gateway rejected the key."
    elif status == 429:
        code, message = GatewayErrorCode.RATE_LIMITED, "The gateway rate-limited the request."
        retry_after = _retry_after_seconds(response)
    elif status == 504:
        code, message = GatewayErrorCode.TIMEOUT, "The gateway or its provider timed out."
    elif 400 <= status < 500:
        code, message = GatewayErrorCode.REQUEST_REJECTED, "The gateway rejected the request."
    elif status >= 500:
        code, message = GatewayErrorCode.UNAVAILABLE, "The gateway or its provider failed."
    else:
        code, message = GatewayErrorCode.UNEXPECTED_STATUS, "The gateway answered unexpectedly."
    return GatewayCallError(
        code,
        message,
        may_have_executed=may_have_executed,
        status_code=status,
        gateway_error_type=error_type,
        gateway_error_code=error_code,
        retry_after_seconds=retry_after,
    )


def _may_have_executed(status: int) -> bool:
    # Every 4xx the gateway sends is a refusal before or instead of generation. A
    # 5xx, or a 200 the runtime cannot use, can follow a provider call that ran and
    # was charged.
    return not 400 <= status < 500


def _gateway_error(payload: bytes) -> tuple[str | None, str | None]:
    try:
        error = _ErrorBody.model_validate_json(payload).error
    except ValidationError:
        return None, None
    return _identifier(error.type), _identifier(error.code)


def _identifier(value: str | None) -> str | None:
    return value if value is not None and _ERROR_IDENTIFIER.fullmatch(value) else None


def _safe_request_id(value: str | None) -> str | None:
    return value if value is not None and _REQUEST_ID.fullmatch(value) else None


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "").strip()
    if not (value.isascii() and value.isdigit()) or len(value) > 10:
        return None
    return min(int(value), _MAX_RETRY_AFTER_SECONDS)


def _transport_error(exc: httpx.RequestError) -> GatewayCallError:
    # Only failures before the request was sent prove the gateway did not run it.
    may_have_executed = not isinstance(exc, _NOT_SENT_ERRORS)
    if isinstance(exc, httpx.TimeoutException):
        return GatewayCallError(
            GatewayErrorCode.TIMEOUT,
            "The gateway did not respond in time.",
            may_have_executed=may_have_executed,
        )
    if isinstance(exc, httpx.DecodingError):
        return GatewayCallError(
            GatewayErrorCode.MALFORMED_RESPONSE,
            "The gateway response could not be decoded.",
            may_have_executed=True,
        )
    return GatewayCallError(
        GatewayErrorCode.UNAVAILABLE,
        "Could not communicate with the gateway.",
        may_have_executed=may_have_executed,
    )
