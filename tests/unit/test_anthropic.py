from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
import pytest
from pydantic import SecretStr

from gateway.core.config import AnthropicProviderConfig
from gateway.errors import (
    InvalidRequestError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.base import ProviderRequest, StreamEnd, StreamEvent, TextDelta, Usage
from gateway.schemas.completions import ChatMessage
from tests.fakes import SSE_HEADERS, UpstreamHandler, UpstreamStub, sse_events

UPSTREAM_KEY = "sk-ant-upstream-secret"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 2},
}


def make_request(
    *messages: tuple[Literal["system", "user", "assistant"], str], **overrides: Any
) -> ProviderRequest:
    values: dict[str, Any] = {
        "model": "upstream-model",
        "messages": tuple(ChatMessage(role=role, content=text) for role, text in messages)
        or (ChatMessage(role="user", content="hi"),),
        "max_tokens": 256,
        "temperature": None,
        "top_p": None,
        "stop": (),
        "include_usage": False,
    }
    values.update(overrides)
    return ProviderRequest(**values)


def make_provider(
    handler: UpstreamHandler, *, api_version: str = "2026-10-21"
) -> tuple[AnthropicProvider, UpstreamStub]:
    stub = UpstreamStub(handler)
    config = AnthropicProviderConfig.model_validate(
        {
            "type": "anthropic",
            "base_url": "https://anthropic.test",
            "api_key_env": "ANTHROPIC_API_KEY",
            "api_version": api_version,
        }
    )
    provider = AnthropicProvider.from_config(
        "anthropic", config, SecretStr(UPSTREAM_KEY), transport=stub
    )
    return provider, stub


def respond(body: object = MESSAGE) -> UpstreamHandler:
    return lambda _: httpx.Response(200, json=body)


def respond_stream(*events: tuple[str, object]) -> UpstreamHandler:
    return lambda _: httpx.Response(200, headers=SSE_HEADERS, content=sse_events(*events))


async def collect_into(events: AsyncIterator[StreamEvent], received: list[StreamEvent]) -> None:
    async for event in events:
        received.append(event)


class TestRequestTranslation:
    async def test_sends_messages_api_request(self) -> None:
        provider, stub = make_provider(respond())

        await provider.complete(
            make_request(
                ("system", "be brief"),
                ("system", "use English"),
                ("user", "hi"),
                ("assistant", "hello"),
                ("user", "bye"),
                stop=("END",),
            )
        )

        [request] = stub.requests
        assert str(request.url) == "https://anthropic.test/v1/messages"
        assert request.headers["x-api-key"] == UPSTREAM_KEY
        assert request.headers["anthropic-version"] == "2026-10-21"
        assert "authorization" not in request.headers
        assert stub.json_bodies() == [
            {
                "model": "upstream-model",
                "max_tokens": 256,
                "stream": False,
                "system": [
                    {"type": "text", "text": "be brief"},
                    {"type": "text", "text": "use English"},
                ],
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "bye"},
                ],
                "stop_sequences": ["END"],
            }
        ]

    async def test_omits_system_when_absent(self) -> None:
        provider, stub = make_provider(respond())

        await provider.complete(make_request(("user", "hi")))

        assert "system" not in stub.json_bodies()[0]

    @pytest.mark.parametrize(
        ("request_", "error", "param"),
        [
            (
                make_request(("user", "hi"), ("system", "late"), ("user", "x")),
                UnsupportedCapabilityError,
                "messages.1.role",
            ),
            (make_request(("system", "only")), InvalidRequestError, "messages"),
            (make_request(("user", "hi"), max_tokens=None), InvalidRequestError, "max_tokens"),
            (make_request(temperature=0.5), UnsupportedCapabilityError, "temperature"),
            (make_request(top_p=0.9), UnsupportedCapabilityError, "top_p"),
        ],
    )
    async def test_rejects_requests_that_cannot_be_translated(
        self, request_: ProviderRequest, error: type[Exception], param: str
    ) -> None:
        provider, stub = make_provider(respond())

        with pytest.raises(error) as exc_info:
            await provider.complete(request_)

        assert getattr(exc_info.value, "param", None) == param
        assert stub.requests == []

    async def test_forwards_sampling_parameters_on_older_api_version(self) -> None:
        provider, stub = make_provider(respond(), api_version="2023-06-01")

        await provider.complete(make_request(temperature=0.5, top_p=0.9))

        [body] = stub.json_bodies()
        assert (body["temperature"], body["top_p"]) == (0.5, 0.9)
        assert stub.requests[0].headers["anthropic-version"] == "2023-06-01"

    async def test_rejects_temperature_outside_the_provider_range(self) -> None:
        provider, stub = make_provider(respond(), api_version="2023-06-01")

        with pytest.raises(UnsupportedCapabilityError) as exc_info:
            await provider.complete(make_request(temperature=1.5))

        assert exc_info.value.code == "unsupported_parameter_value"
        assert stub.requests == []

    async def test_stream_rejections_happen_before_any_request(self) -> None:
        provider, stub = make_provider(respond_stream())

        with pytest.raises(UnsupportedCapabilityError):
            await anext(provider.stream(make_request(temperature=0.5)))

        assert stub.requests == []


class TestComplete:
    async def test_normalizes_text_and_usage_including_cached_input(self) -> None:
        body = {
            **MESSAGE,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Hel"},
                {"type": "text", "text": "lo"},
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 100,
            },
        }
        provider, _ = make_provider(respond(body))

        completion = await provider.complete(make_request())

        assert completion.content == "Hello"
        assert completion.usage == Usage(prompt_tokens=115, completion_tokens=2, total_tokens=117)
        assert completion.provider_response_id == "msg_1"

    @pytest.mark.parametrize(
        ("stop_reason", "finish_reason"),
        [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("model_context_window_exceeded", "length"),
            ("refusal", "content_filter"),
            ("pause_turn", None),
            (None, None),
        ],
    )
    async def test_maps_stop_reasons(
        self, stop_reason: str | None, finish_reason: str | None
    ) -> None:
        provider, _ = make_provider(respond({**MESSAGE, "stop_reason": stop_reason}))

        assert (await provider.complete(make_request())).finish_reason == finish_reason

    async def test_usage_is_not_invented_when_incomplete(self) -> None:
        provider, _ = make_provider(respond({**MESSAGE, "usage": {"input_tokens": 3}}))

        assert (await provider.complete(make_request())).usage is None

    @pytest.mark.parametrize(
        "content",
        [
            [{"type": "tool_use", "id": "t", "name": "f", "input": {}}],
            [{"type": "text"}],
            "hello",
        ],
    )
    async def test_rejects_content_the_gateway_cannot_represent(self, content: object) -> None:
        provider, _ = make_provider(respond({**MESSAGE, "content": content}))

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await provider.complete(make_request())

        assert exc_info.value.code == "malformed_provider_response"

    @pytest.mark.parametrize(
        ("status", "error_type", "expected"),
        [
            (529, "overloaded_error", ProviderUnavailableError),
            (429, "rate_limit_error", ProviderRateLimitError),
            (401, "authentication_error", ProviderConfigurationError),
        ],
    )
    async def test_maps_error_responses_without_exposing_details(
        self, status: int, error_type: str, expected: type[ProviderError]
    ) -> None:
        body = {"type": "error", "error": {"type": error_type, "message": f"key {UPSTREAM_KEY}"}}
        provider, _ = make_provider(lambda _: httpx.Response(status, json=body))

        with pytest.raises(expected) as exc_info:
            await provider.complete(make_request())

        assert exc_info.value.upstream_error_type == error_type
        assert UPSTREAM_KEY not in str(exc_info.value)


MESSAGE_START = (
    "message_start",
    {
        "type": "message_start",
        "message": {"id": "msg_1", "usage": {"input_tokens": 7, "output_tokens": 1}},
    },
)


def text_block(index: int, text: str) -> list[tuple[str, object]]:
    return [
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


def message_end(stop_reason: str = "end_turn", output_tokens: int = 4) -> list[tuple[str, object]]:
    return [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


class TestStream:
    async def test_yields_text_and_end_skipping_thinking_and_pings(self) -> None:
        thinking = [
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "s"},
                },
            ),
        ]
        provider, stub = make_provider(
            respond_stream(
                MESSAGE_START,
                ("ping", {"type": "ping"}),
                *thinking,
                *text_block(1, "Hel"),
                *text_block(2, "lo"),
                ("future_event", {"type": "future_event"}),
                *message_end("max_tokens"),
            )
        )
        received: list[StreamEvent] = []

        await collect_into(provider.stream(make_request()), received)

        assert received == [
            TextDelta("Hel"),
            TextDelta("lo"),
            StreamEnd(finish_reason="length", usage=Usage(7, 4, 11)),
        ]
        assert stub.json_bodies()[0]["stream"] is True
        assert stub.requests[0].headers["accept"] == "text/event-stream"

    @pytest.mark.parametrize(
        ("error_type", "expected"),
        [
            ("overloaded_error", ProviderUnavailableError),
            ("rate_limit_error", ProviderRateLimitError),
        ],
    )
    async def test_error_event_before_output_is_raised_from_the_first_read(
        self, error_type: str, expected: type[ProviderError]
    ) -> None:
        error = ("error", {"type": "error", "error": {"type": error_type, "message": "busy"}})
        provider, _ = make_provider(respond_stream(MESSAGE_START, error))

        with pytest.raises(expected):
            await anext(provider.stream(make_request()))

    async def test_error_event_after_partial_output(self) -> None:
        error = ("error", {"type": "error", "error": {"type": "api_error"}})
        provider, _ = make_provider(respond_stream(MESSAGE_START, *text_block(0, "partial"), error))
        received: list[StreamEvent] = []

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await collect_into(provider.stream(make_request()), received)

        assert received == [TextDelta("partial")]
        assert exc_info.value.code == "provider_stream_error"

    async def test_stream_ending_without_message_stop_is_incomplete(self) -> None:
        provider, _ = make_provider(respond_stream(MESSAGE_START, *text_block(0, "partial")))
        received: list[StreamEvent] = []

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await collect_into(provider.stream(make_request()), received)

        assert received == [TextDelta("partial")]
        assert exc_info.value.code == "provider_stream_incomplete"

    @pytest.mark.parametrize(
        "events",
        [
            [
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "tool_use"},
                    },
                )
            ],
            [
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 5,
                        "delta": {"type": "text_delta", "text": "x"},
                    },
                )
            ],
            [("message_start", {"type": "message_start"})],
            [("message", ["not", "an", "object"])],
        ],
    )
    async def test_malformed_streams(self, events: list[tuple[str, object]]) -> None:
        provider, _ = make_provider(respond_stream(*events))

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await collect_into(provider.stream(make_request()), [])

        assert exc_info.value.code == "malformed_provider_response"

    async def test_error_status_before_stream(self) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(529, json={"type": "error"}))

        with pytest.raises(ProviderUnavailableError):
            await anext(provider.stream(make_request()))
