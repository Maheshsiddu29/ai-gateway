from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from gateway.core.config import OpenAICompatibleProviderConfig
from gateway.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from gateway.providers.base import ProviderRequest, StreamEnd, StreamEvent, TextDelta, Usage
from gateway.providers.openai_compatible import OpenAICompatibleProvider
from gateway.schemas.completions import ChatMessage
from tests.fakes import SSE_HEADERS, ControlledStream, UpstreamHandler, UpstreamStub, sse_body

UPSTREAM_KEY = "upstream-secret-key"
REQUEST = ProviderRequest(
    model="upstream-model",
    messages=(
        ChatMessage(role="system", content="be brief"),
        ChatMessage(role="user", content="hi"),
    ),
    max_tokens=None,
    temperature=None,
    top_p=None,
    stop=(),
    include_usage=False,
)
COMPLETION = {
    "id": "resp-1",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}


def make_provider(
    handler: UpstreamHandler,
    *,
    api_key: str | None = UPSTREAM_KEY,
    base_url: str = "https://llm.test/v1",
) -> tuple[OpenAICompatibleProvider, UpstreamStub]:
    stub = UpstreamStub(handler)
    config = OpenAICompatibleProviderConfig.model_validate(
        {"type": "openai_compatible", "base_url": base_url}
    )
    key = SecretStr(api_key) if api_key is not None else None
    return OpenAICompatibleProvider.from_config("openai", config, key, transport=stub), stub


def chunk(content: str | None = None, finish_reason: str | None = None) -> dict[str, object]:
    delta = {} if content is None else {"content": content}
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


async def collect(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in events]


async def collect_into(events: AsyncIterator[StreamEvent], received: list[StreamEvent]) -> None:
    async for event in events:
        received.append(event)


class TestComplete:
    async def test_sends_only_supported_fields_to_the_configured_endpoint(self) -> None:
        provider, stub = make_provider(lambda _: httpx.Response(200, json=COMPLETION))

        await provider.complete(REQUEST)

        [request] = stub.requests
        assert request.method == "POST"
        assert str(request.url) == "https://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
        assert request.headers["content-type"] == "application/json"
        assert stub.json_bodies() == [
            {
                "model": "upstream-model",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hi"},
                ],
                "stream": False,
            }
        ]

    async def test_forwards_generation_parameters(self) -> None:
        provider, stub = make_provider(lambda _: httpx.Response(200, json=COMPLETION))
        request = ProviderRequest(
            model="m",
            messages=REQUEST.messages,
            max_tokens=64,
            temperature=0.2,
            top_p=0.9,
            stop=("END", "STOP"),
            include_usage=False,
        )

        await provider.complete(request)

        [body] = stub.json_bodies()
        assert body["max_tokens"] == 64
        assert body["temperature"] == 0.2
        assert body["top_p"] == 0.9
        assert body["stop"] == ["END", "STOP"]

    async def test_omits_authorization_when_no_key_is_configured(self) -> None:
        provider, stub = make_provider(lambda _: httpx.Response(200, json=COMPLETION), api_key=None)

        await provider.complete(REQUEST)

        assert "authorization" not in stub.requests[0].headers

    async def test_normalizes_the_response(self) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(200, json=COMPLETION))

        completion = await provider.complete(REQUEST)

        assert completion.content == "hello"
        assert completion.finish_reason == "stop"
        assert completion.usage == Usage(prompt_tokens=5, completion_tokens=1, total_tokens=6)
        assert completion.provider_response_id == "resp-1"

    async def test_usage_is_not_invented_when_missing(self) -> None:
        body = {key: value for key, value in COMPLETION.items() if key != "usage"}
        provider, _ = make_provider(lambda _: httpx.Response(200, json=body))

        assert (await provider.complete(REQUEST)).usage is None

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b'{"choices": []}',
            b'{"choices": [{"message": {"content": null}}]}',
            b'{"choices": [{"message": {"content": [{"type": "text"}]}}]}',
            b'{"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": "a"}}',
        ],
    )
    async def test_malformed_responses(self, body: bytes) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(200, content=body))

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await provider.complete(REQUEST)

        assert exc_info.value.code == "malformed_provider_response"

    async def test_does_not_follow_redirects(self) -> None:
        provider, stub = make_provider(
            lambda _: httpx.Response(307, headers={"location": "http://169.254.169.254/"})
        )

        with pytest.raises(ProviderUnavailableError):
            await provider.complete(REQUEST)

        assert len(stub.requests) == 1

    async def test_rate_limit(self) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(429, headers={"retry-after": "3"}))

        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.complete(REQUEST)

        assert exc_info.value.retry_after_seconds == 3

    async def test_errors_never_contain_the_provider_credential(self) -> None:
        provider, _ = make_provider(
            lambda request: httpx.Response(
                401, json={"error": {"message": f"bad key {request.headers['authorization']}"}}
            )
        )

        with pytest.raises(ProviderConfigurationError) as exc_info:
            await provider.complete(REQUEST)

        assert UPSTREAM_KEY not in str(exc_info.value)
        assert UPSTREAM_KEY not in exc_info.value.message

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (httpx.ConnectError("connection refused"), ProviderUnavailableError),
            (httpx.ConnectTimeout("timed out"), ProviderTimeoutError),
            (httpx.ReadTimeout("timed out"), ProviderTimeoutError),
        ],
    )
    async def test_transport_errors(
        self, exc: httpx.TransportError, expected: type[ProviderError]
    ) -> None:
        def fail(_: httpx.Request) -> httpx.Response:
            raise exc

        provider, _ = make_provider(fail)

        with pytest.raises(expected):
            await provider.complete(REQUEST)

    async def test_aclose_closes_the_http_client(self) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(200, json=COMPLETION))

        await provider.aclose()

        with pytest.raises(RuntimeError):
            await provider.complete(REQUEST)


class TestStream:
    async def test_yields_deltas_then_end_with_usage(self) -> None:
        body = sse_body(
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
            chunk("Hel"),
            chunk("lo"),
            chunk(finish_reason="stop"),
            {
                "choices": [],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )
        provider, _ = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=body)
        )

        events = await collect(provider.stream(REQUEST))

        assert events == [
            TextDelta("Hel"),
            TextDelta("lo"),
            StreamEnd(finish_reason="stop", usage=Usage(3, 2, 5)),
        ]

    @pytest.mark.parametrize("include_usage", [False, True])
    async def test_requests_a_stream(self, include_usage: bool) -> None:
        body = sse_body(chunk("x", finish_reason="stop"))
        provider, stub = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=body)
        )
        request = ProviderRequest(
            model="m",
            messages=REQUEST.messages,
            max_tokens=None,
            temperature=None,
            top_p=None,
            stop=(),
            include_usage=include_usage,
        )

        await collect(provider.stream(request))

        [body_sent] = stub.json_bodies()
        assert body_sent["stream"] is True
        assert stub.requests[0].headers["accept"] == "text/event-stream"
        assert ("stream_options" in body_sent) is include_usage

    async def test_error_status_is_raised_before_any_event(self) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(429, json={"error": {}}))

        with pytest.raises(ProviderRateLimitError):
            await anext(provider.stream(REQUEST))

    async def test_rejects_a_non_stream_response(self) -> None:
        provider, _ = make_provider(lambda _: httpx.Response(200, json=COMPLETION))

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await anext(provider.stream(REQUEST))

        assert exc_info.value.code == "malformed_provider_response"

    async def test_accepts_end_of_stream_without_done_after_a_finish_reason(self) -> None:
        body = sse_body(chunk("x"), chunk(finish_reason="length"), done=False)
        provider, _ = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=body)
        )

        events = await collect(provider.stream(REQUEST))

        assert events[-1] == StreamEnd(finish_reason="length", usage=None)

    async def test_end_of_stream_before_completion_is_an_error(self) -> None:
        body = sse_body(chunk("partial"), done=False)
        provider, _ = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=body)
        )
        received: list[StreamEvent] = []

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await collect_into(provider.stream(REQUEST), received)

        assert received == [TextDelta("partial")]
        assert exc_info.value.code == "provider_stream_incomplete"

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            (b"data: {not json}\n\n", "malformed_provider_response"),
            (b'data: {"choices": "x"}\n\n', "malformed_provider_response"),
            (b'data: {"error": {"type": "server_error"}}\n\n', "provider_stream_error"),
        ],
    )
    async def test_invalid_chunks(self, body: bytes, code: str) -> None:
        provider, _ = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=body)
        )

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await collect(provider.stream(REQUEST))

        assert exc_info.value.code == code

    async def test_read_timeout_after_partial_output(self) -> None:
        stream = ControlledStream(sse_body(chunk("Hel"), done=False), httpx.ReadTimeout("slow"))
        provider, _ = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=stream)
        )
        received: list[StreamEvent] = []

        with pytest.raises(ProviderTimeoutError):
            await collect_into(provider.stream(REQUEST), received)

        assert received == [TextDelta("Hel")]
        assert stream.closed.is_set()

    async def test_closing_the_stream_early_releases_the_upstream_response(self) -> None:
        stream = ControlledStream(sse_body(chunk("Hel"), done=False))
        provider, _ = make_provider(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=stream)
        )
        events = provider.stream(REQUEST)

        assert await anext(events) == TextDelta("Hel")
        await events.aclose()

        assert stream.closed.is_set()
