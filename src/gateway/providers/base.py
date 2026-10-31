"""The provider adapter interface and the provider-neutral result types.

Adapters translate a ``ProviderRequest`` into their upstream wire format and
normalize upstream responses into ``Completion`` or a sequence of ``StreamEvent``
values. All provider-specific serialization stays inside the adapters.

Cancellation uses asyncio task cancellation. Adapters must release upstream
connections when cancelled, and when a stream generator is closed early.
"""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Protocol

from gateway.schemas.completions import ChatMessage


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    messages: Sequence[ChatMessage]
    max_tokens: int | None
    temperature: float | None
    top_p: float | None
    stop: tuple[str, ...]
    include_usage: bool


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    billing_dimensions_supported: bool = True


@dataclass(frozen=True)
class Completion:
    content: str
    # None when the provider reported a stop reason the gateway does not map.
    finish_reason: str | None
    # None when the provider did not report token usage.
    usage: Usage | None
    provider_response_id: str | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class StreamEnd:
    """The final event of a stream that completed successfully."""

    finish_reason: str | None
    usage: Usage | None


type StreamEvent = TextDelta | StreamEnd


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    def validate(self, request: ProviderRequest) -> None:
        """Raise a ``GatewayError`` if this provider cannot run the request faithfully.

        Called before admission, so that requests that would be rejected consume no
        quota and are never sent upstream. Performs no I/O.
        """
        ...

    async def complete(self, request: ProviderRequest) -> Completion:
        """Return a full completion or raise a ``GatewayError``."""
        ...

    def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        """Yield text deltas followed by exactly one ``StreamEnd``.

        Errors detected before the first event (rejected requests, upstream error
        statuses) are raised from the first iteration. A stream that ends without
        a clean upstream termination raises instead of yielding ``StreamEnd``.
        """
        ...

    async def aclose(self) -> None: ...
