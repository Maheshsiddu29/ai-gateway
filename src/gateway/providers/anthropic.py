"""Adapter for the Anthropic Messages API.

Translation from the gateway's OpenAI-style request:

* Leading ``system`` messages become top-level ``system`` text blocks, one per
  message. A system message after the conversation has started has no equivalent
  in the Messages API and is rejected.
* ``max_tokens`` is required by the Messages API. When neither the client nor the
  model configuration supplies it, the request is rejected.
* ``stop`` becomes ``stop_sequences``.
* Sampling parameters depend on the API version. On ``2026-10-21`` the API rejects
  ``temperature`` and ``top_p``, so the gateway rejects them before sending. On
  ``2023-06-01`` they are forwarded, with ``temperature`` limited to the API's
  0-1 range.

Responses: text blocks are concatenated. ``thinking`` and ``redacted_thinking``
blocks, which models with adaptive thinking may return without being asked, are
not part of the gateway's output and are skipped. Any other block type, such as
``tool_use``, cannot be represented and is treated as a malformed response.
"""

import json
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from gateway.core.config import AnthropicApiVersion, AnthropicProviderConfig
from gateway.errors import (
    InvalidRequestError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from gateway.providers.base import (
    Completion,
    ProviderRequest,
    StreamEnd,
    StreamEvent,
    TextDelta,
    Usage,
)
from gateway.providers.http import (
    create_client,
    error_from_status,
    error_from_transport,
    incomplete_stream_error,
    is_event_stream,
    malformed_response_error,
)
from gateway.providers.sse import ServerSentEvent, iter_sse

_MESSAGES_PATH = "/v1/messages"
_SAMPLING_REMOVED_VERSION: AnthropicApiVersion = "2026-10-21"
_MAX_TEMPERATURE = 1.0
_IGNORED_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}


class _Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def merged(self, update: "_Usage") -> "_Usage":
        return self.model_copy(update=update.model_dump(exclude_none=True))

    def normalize(self) -> Usage | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        # Cached input is reported separately from input_tokens; all of it was
        # part of the prompt.
        prompt = (
            self.input_tokens
            + (self.cache_creation_input_tokens or 0)
            + (self.cache_read_input_tokens or 0)
        )
        return Usage(prompt, self.output_tokens, prompt + self.output_tokens)


class _ContentBlock(BaseModel):
    type: str
    text: str | None = None


class _Message(BaseModel):
    id: str | None = None
    content: list[_ContentBlock]
    stop_reason: str | None = None
    usage: _Usage = Field(default_factory=_Usage)


class _StartedMessage(BaseModel):
    usage: _Usage = Field(default_factory=_Usage)


class _MessageStart(BaseModel):
    message: _StartedMessage


class _BlockStart(BaseModel):
    index: int
    content_block: _ContentBlock


class _Delta(BaseModel):
    type: str
    text: str | None = None


class _BlockDelta(BaseModel):
    index: int
    delta: _Delta


class _StopDelta(BaseModel):
    stop_reason: str | None = None


class _MessageDelta(BaseModel):
    delta: _StopDelta = Field(default_factory=_StopDelta)
    usage: _Usage = Field(default_factory=_Usage)


class _ErrorBody(BaseModel):
    type: str | None = None


class _StreamError(BaseModel):
    error: _ErrorBody = Field(default_factory=_ErrorBody)


class AnthropicProvider:
    def __init__(
        self, name: str, client: httpx.AsyncClient, *, api_version: AnthropicApiVersion
    ) -> None:
        self._name = name
        self._client = client
        self._api_version = api_version

    @classmethod
    def from_config(
        cls,
        name: str,
        config: AnthropicProviderConfig,
        api_key: SecretStr | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "AnthropicProvider":
        if api_key is None:
            raise ValueError("the Anthropic provider requires an API key")
        headers = {"x-api-key": api_key.get_secret_value(), "anthropic-version": config.api_version}
        client = create_client(config, headers=headers, transport=transport)
        return cls(name, client, api_version=config.api_version)

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, request: ProviderRequest) -> Completion:
        payload = self._build_payload(request, stream=False)
        try:
            response = await self._client.post(
                _MESSAGES_PATH, json=payload, headers={"accept": "application/json"}
            )
        except httpx.TransportError as exc:
            raise error_from_transport(exc) from exc
        if not response.is_success:
            raise error_from_status(response)

        try:
            message = _Message.model_validate_json(response.content)
        except ValidationError:
            raise malformed_response_error() from None
        return Completion(
            content=_message_text(message.content),
            finish_reason=_finish_reason(message.stop_reason),
            usage=message.usage.normalize(),
            provider_response_id=message.id,
        )

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        payload = self._build_payload(request, stream=True)
        try:
            async with self._client.stream(
                "POST", _MESSAGES_PATH, json=payload, headers={"accept": "text/event-stream"}
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise error_from_status(response)
                if not is_event_stream(response):
                    raise malformed_response_error()
                async for event in _parse_stream(iter_sse(response.aiter_bytes())):
                    yield event
        except httpx.TransportError as exc:
            raise error_from_transport(exc) from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_payload(self, request: ProviderRequest, *, stream: bool) -> dict[str, Any]:
        system, messages = _split_system_messages(request)
        if request.max_tokens is None:
            raise InvalidRequestError(
                "max_tokens is required for this model.",
                code="max_tokens_required",
                param="max_tokens",
            )

        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
            "stream": stream,
        }
        if system:
            payload["system"] = [{"type": "text", "text": text} for text in system]
        if request.stop:
            payload["stop_sequences"] = list(request.stop)
        payload.update(self._sampling_parameters(request))
        return payload

    def _sampling_parameters(self, request: ProviderRequest) -> dict[str, float]:
        parameters = {
            name: value
            for name, value in (("temperature", request.temperature), ("top_p", request.top_p))
            if value is not None
        }
        if parameters and self._api_version == _SAMPLING_REMOVED_VERSION:
            name = next(iter(parameters))
            raise UnsupportedCapabilityError(
                f"Parameter '{name}' is not supported by this model's provider.",
                code="unsupported_parameter",
                param=name,
            )
        if parameters.get("temperature", 0.0) > _MAX_TEMPERATURE:
            raise UnsupportedCapabilityError(
                f"This model's provider accepts temperature values up to {_MAX_TEMPERATURE}.",
                code="unsupported_parameter_value",
                param="temperature",
            )
        return parameters


def _split_system_messages(
    request: ProviderRequest,
) -> tuple[list[str], list[dict[str, str]]]:
    system: list[str] = []
    messages: list[dict[str, str]] = []
    for index, message in enumerate(request.messages):
        if message.role != "system":
            messages.append({"role": message.role, "content": message.content})
        elif messages:
            raise UnsupportedCapabilityError(
                "This model's provider only accepts system messages before the conversation.",
                code="unsupported_message_order",
                param=f"messages.{index}.role",
            )
        else:
            system.append(message.content)
    if not messages:
        raise InvalidRequestError(
            "At least one user or assistant message is required for this model.",
            code="missing_conversation_messages",
            param="messages",
        )
    return system, messages


def _message_text(blocks: list[_ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.type in _IGNORED_BLOCK_TYPES:
            continue
        if block.type != "text" or block.text is None:
            raise malformed_response_error()
        parts.append(block.text)
    return "".join(parts)


def _finish_reason(stop_reason: str | None) -> str | None:
    return _FINISH_REASONS.get(stop_reason) if stop_reason is not None else None


async def _parse_stream(events: AsyncIterable[ServerSentEvent]) -> AsyncGenerator[StreamEvent]:
    """Normalize Messages API stream events.

    The stream is complete only at ``message_stop``. Unknown event types are
    skipped, as the API documentation asks clients to do.
    """
    usage = _Usage()
    stop_reason: str | None = None
    block_types: dict[int, str] = {}
    async for event in events:
        try:
            data = json.loads(event.data)
            kind = data.get("type") if isinstance(data, dict) else None
            match kind:
                case "message_start":
                    usage = usage.merged(_MessageStart.model_validate(data).message.usage)
                case "content_block_start":
                    start = _BlockStart.model_validate(data)
                    if start.content_block.type not in _IGNORED_BLOCK_TYPES | {"text"}:
                        raise malformed_response_error()
                    block_types[start.index] = start.content_block.type
                case "content_block_delta":
                    delta = _BlockDelta.model_validate(data)
                    text = _delta_text(delta, block_types)
                    if text:
                        yield TextDelta(text)
                case "message_delta":
                    message_delta = _MessageDelta.model_validate(data)
                    stop_reason = message_delta.delta.stop_reason or stop_reason
                    usage = usage.merged(message_delta.usage)
                case "message_stop":
                    yield StreamEnd(
                        finish_reason=_finish_reason(stop_reason), usage=usage.normalize()
                    )
                    return
                case "error":
                    raise _stream_error(_StreamError.model_validate(data).error.type)
                case None:
                    raise malformed_response_error()
        except (json.JSONDecodeError, ValidationError):
            raise malformed_response_error() from None
    raise incomplete_stream_error()


def _delta_text(delta: _BlockDelta, block_types: dict[int, str]) -> str | None:
    block_type = block_types.get(delta.index)
    if block_type in _IGNORED_BLOCK_TYPES:
        return None
    if block_type != "text" or delta.delta.type != "text_delta" or delta.delta.text is None:
        raise malformed_response_error()
    return delta.delta.text


def _stream_error(upstream_error_type: str | None) -> ProviderError:
    if upstream_error_type == "rate_limit_error":
        return ProviderRateLimitError(
            "The provider's rate limit was exceeded.", upstream_error_type=upstream_error_type
        )
    return ProviderUnavailableError(
        "The provider reported an error during the stream.",
        code="provider_stream_error",
        upstream_error_type=upstream_error_type,
    )
