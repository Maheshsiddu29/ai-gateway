"""Adapter for servers implementing the OpenAI Chat Completions HTTP API.

Only the request fields the gateway supports are sent, and only fields that are
set are included, so servers that implement a subset of the API are not sent
options they may reject. ``stream_options`` is sent only when the client asked
for usage in a stream.
"""

from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from gateway.core.config import OpenAICompatibleProviderConfig
from gateway.errors import ProviderUnavailableError
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

_CHAT_COMPLETIONS_PATH = "/chat/completions"
_STREAM_DONE = "[DONE]"


class _Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int | None = None

    def normalize(self) -> Usage:
        total = self.total_tokens
        if total is None:
            total = self.prompt_tokens + self.completion_tokens
        return Usage(self.prompt_tokens, self.completion_tokens, total)


class _ResponseMessage(BaseModel):
    content: str


class _Choice(BaseModel):
    message: _ResponseMessage
    finish_reason: str | None = None


class _ChatCompletion(BaseModel):
    id: str | None = None
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


class _Delta(BaseModel):
    content: str | None = None


class _ChunkChoice(BaseModel):
    index: int = 0
    delta: _Delta = Field(default_factory=_Delta)
    finish_reason: str | None = None


class _ChunkError(BaseModel):
    type: str | None = None


class _Chunk(BaseModel):
    choices: list[_ChunkChoice] = Field(default_factory=list)
    usage: _Usage | None = None
    error: _ChunkError | None = None


class OpenAICompatibleProvider:
    def __init__(self, name: str, client: httpx.AsyncClient) -> None:
        self._name = name
        self._client = client

    @classmethod
    def from_config(
        cls,
        name: str,
        config: OpenAICompatibleProviderConfig,
        api_key: SecretStr | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "OpenAICompatibleProvider":
        headers = {}
        if api_key is not None:
            headers["authorization"] = f"Bearer {api_key.get_secret_value()}"
        return cls(name, create_client(config, headers=headers, transport=transport))

    @property
    def name(self) -> str:
        return self._name

    def validate(self, request: ProviderRequest) -> None:
        # Every field of the gateway's request subset maps directly onto this API.
        return

    async def complete(self, request: ProviderRequest) -> Completion:
        try:
            response = await self._client.post(
                _CHAT_COMPLETIONS_PATH,
                json=_build_payload(request, stream=False),
                headers={"accept": "application/json"},
            )
        except httpx.TransportError as exc:
            raise error_from_transport(exc) from exc
        if not response.is_success:
            raise error_from_status(response)

        try:
            parsed = _ChatCompletion.model_validate_json(response.content)
        except ValidationError:
            raise malformed_response_error() from None
        choice = parsed.choices[0]
        return Completion(
            content=choice.message.content,
            finish_reason=choice.finish_reason,
            usage=parsed.usage.normalize() if parsed.usage else None,
            provider_response_id=parsed.id,
        )

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        try:
            async with self._client.stream(
                "POST",
                _CHAT_COMPLETIONS_PATH,
                json=_build_payload(request, stream=True),
                headers={"accept": "text/event-stream"},
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


def _build_payload(request: ProviderRequest, *, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "stream": stream,
    }
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.stop:
        payload["stop"] = list(request.stop)
    if stream and request.include_usage:
        payload["stream_options"] = {"include_usage": True}
    return payload


async def _parse_stream(
    events: AsyncIterable[ServerSentEvent],
) -> AsyncGenerator[StreamEvent]:
    """Normalize chat completion chunks.

    The stream is complete at ``data: [DONE]``. Some compatible servers close the
    connection without sending ``[DONE]``; that is accepted only if a finish reason
    was already received, otherwise the stream is treated as interrupted.
    The finish reason and usage arrive in separate chunks, so ``StreamEnd`` is
    emitted only once the stream has ended.
    """
    finish_reason: str | None = None
    usage: Usage | None = None
    async for event in events:
        if event.data == _STREAM_DONE:
            yield StreamEnd(finish_reason=finish_reason, usage=usage)
            return
        try:
            chunk = _Chunk.model_validate_json(event.data)
        except ValidationError:
            raise malformed_response_error() from None
        if chunk.error is not None:
            raise ProviderUnavailableError(
                "The provider reported an error during the stream.",
                code="provider_stream_error",
                upstream_error_type=chunk.error.type,
            )
        if chunk.usage is not None:
            usage = chunk.usage.normalize()
        for choice in chunk.choices:
            if choice.index != 0:
                continue
            if choice.delta.content:
                yield TextDelta(choice.delta.content)
            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason

    if finish_reason is None:
        raise incomplete_stream_error()
    yield StreamEnd(finish_reason=finish_reason, usage=usage)
