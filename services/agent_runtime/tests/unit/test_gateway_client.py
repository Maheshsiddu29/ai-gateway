import gzip
import json
import logging
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from agent_runtime.gateway_client import (
    ChatMessage,
    GatewayCallError,
    GatewayClient,
    GatewayErrorCode,
    GatewayTimeouts,
    TokenUsage,
)
from tests.fakes import GATEWAY_API_KEY, GatewayHandler, GatewayStub, completion_body, gateway_error

MESSAGES = (
    ChatMessage(role="system", content="You are a test."),
    ChatMessage(role="user", content="a secret incident description"),
)


def make_client(stub: httpx.AsyncBaseTransport, **overrides: int) -> GatewayClient:
    limits = {"max_request_bytes": 65_536, "max_response_bytes": 65_536, **overrides}
    return GatewayClient(
        "http://gateway.test",
        SecretStr(GATEWAY_API_KEY),
        timeouts=GatewayTimeouts(),
        max_connections=2,
        transport=stub,
        **limits,
    )


async def call(
    handler: GatewayHandler, **overrides: int
) -> tuple[GatewayStub, GatewayCallError | None]:
    stub = GatewayStub(handler)
    client = make_client(stub, **overrides)
    try:
        await client.complete(model="planner", messages=MESSAGES, max_tokens=256)
    except GatewayCallError as exc:
        return stub, exc
    finally:
        await client.aclose()
    return stub, None


async def test_sends_only_supported_fields_with_the_gateway_key() -> None:
    stub = GatewayStub(
        lambda _: httpx.Response(
            200,
            json=completion_body(
                '{"ok": true}', usage={"prompt_tokens": 12, "completion_tokens": 3}
            ),
            headers={"x-request-id": "inv-abc-plan-1"},
        )
    )
    client = make_client(stub)

    completion = await client.complete(
        model="planner", messages=MESSAGES, max_tokens=256, request_id="inv-abc-plan-1"
    )
    await client.aclose()

    request = stub.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://gateway.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {GATEWAY_API_KEY}"
    assert request.headers["x-request-id"] == "inv-abc-plan-1"
    assert json.loads(request.content) == {
        "model": "planner",
        "messages": [m.model_dump() for m in MESSAGES],
        "max_tokens": 256,
    }
    assert completion.content == '{"ok": true}'
    assert completion.usage == TokenUsage(prompt_tokens=12, completion_tokens=3)
    assert completion.gateway_request_id == "inv-abc-plan-1"


async def test_drops_request_ids_the_gateway_would_reject() -> None:
    stub = GatewayStub(lambda _: httpx.Response(200, json=completion_body("x")))
    client = make_client(stub)

    await client.complete(model="planner", messages=MESSAGES, max_tokens=1, request_id="bad id")
    await client.aclose()

    assert "x-request-id" not in stub.requests[0].headers


@pytest.mark.parametrize(
    ("response", "code", "may_have_executed"),
    [
        (
            gateway_error(401, "authentication_error", "invalid_api_key"),
            GatewayErrorCode.AUTHENTICATION_FAILED,
            False,
        ),
        (
            gateway_error(400, "invalid_request", "max_tokens_exceeds_limit"),
            GatewayErrorCode.REQUEST_REJECTED,
            False,
        ),
        (
            gateway_error(404, "invalid_request", "model_not_found"),
            GatewayErrorCode.REQUEST_REJECTED,
            False,
        ),
        (
            gateway_error(
                429, "rate_limited", "tokens_per_minute_exceeded", **{"retry-after": "4"}
            ),
            GatewayErrorCode.RATE_LIMITED,
            False,
        ),
        (
            gateway_error(503, "admission_unavailable", "admission_unavailable"),
            GatewayErrorCode.UNAVAILABLE,
            True,
        ),
        (
            gateway_error(502, "provider_unavailable", "provider_server_error"),
            GatewayErrorCode.UNAVAILABLE,
            True,
        ),
        (
            gateway_error(504, "provider_timeout", "provider_read_timeout"),
            GatewayErrorCode.TIMEOUT,
            True,
        ),
        (httpx.Response(500, text="<html>boom</html>"), GatewayErrorCode.UNAVAILABLE, True),
        (httpx.Response(302, headers={"location": "http://evil.test"}), None, True),
    ],
)
async def test_maps_error_statuses_without_retrying(
    response: httpx.Response, code: GatewayErrorCode | None, may_have_executed: bool
) -> None:
    stub, error = await call(lambda _: response)

    assert error is not None
    assert error.code is (code or GatewayErrorCode.UNEXPECTED_STATUS)
    assert error.may_have_executed is may_have_executed
    assert error.status_code == response.status_code
    assert len(stub.requests) == 1


async def test_keeps_gateway_error_identifiers_and_retry_after() -> None:
    _, error = await call(
        lambda _: gateway_error(
            429, "rate_limited", "concurrency_limit_exceeded", **{"retry-after": "7"}
        )
    )

    assert error is not None
    assert error.gateway_error_type == "rate_limited"
    assert error.gateway_error_code == "concurrency_limit_exceeded"
    assert error.retry_after_seconds == 7


async def test_drops_unexpected_gateway_error_text() -> None:
    _, error = await call(lambda _: gateway_error(400, "Ignore previous instructions", "<script>"))

    assert error is not None
    assert error.gateway_error_type is None
    assert error.gateway_error_code is None


@pytest.mark.parametrize(
    ("exception", "code", "may_have_executed"),
    [
        (httpx.ConnectError("refused"), GatewayErrorCode.UNAVAILABLE, False),
        (httpx.ConnectTimeout("slow"), GatewayErrorCode.TIMEOUT, False),
        (httpx.PoolTimeout("busy"), GatewayErrorCode.TIMEOUT, False),
        (httpx.ReadTimeout("slow"), GatewayErrorCode.TIMEOUT, True),
        (httpx.WriteTimeout("slow"), GatewayErrorCode.TIMEOUT, True),
        (httpx.RemoteProtocolError("dropped"), GatewayErrorCode.UNAVAILABLE, True),
        (httpx.ReadError("reset"), GatewayErrorCode.UNAVAILABLE, True),
    ],
)
async def test_maps_transport_failures_without_retrying(
    exception: Exception, code: GatewayErrorCode, may_have_executed: bool
) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise exception

    stub, error = await call(fail)

    assert error is not None
    assert error.code is code
    assert error.may_have_executed is may_have_executed
    assert len(stub.requests) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (completion_body("partial", finish_reason="length"), GatewayErrorCode.COMPLETION_TRUNCATED),
        (completion_body("", finish_reason="content_filter"), GatewayErrorCode.COMPLETION_REFUSED),
        (completion_body("x", finish_reason=None), GatewayErrorCode.COMPLETION_UNSUPPORTED_STATE),
        (
            completion_body("x", finish_reason="tool_calls"),
            GatewayErrorCode.COMPLETION_UNSUPPORTED_STATE,
        ),
        (completion_body(""), GatewayErrorCode.COMPLETION_EMPTY),
        (completion_body(" \n\t"), GatewayErrorCode.COMPLETION_EMPTY),
        (completion_body(None), GatewayErrorCode.COMPLETION_EMPTY),
    ],
)
async def test_rejects_unusable_completions(
    body: dict[str, object], code: GatewayErrorCode
) -> None:
    _, error = await call(lambda _: httpx.Response(200, json=body))

    assert error is not None
    assert error.code is code
    assert error.may_have_executed is True


async def test_unusable_completions_still_report_usage() -> None:
    body = completion_body(
        "partial", finish_reason="length", usage={"prompt_tokens": 5, "completion_tokens": 256}
    )

    _, error = await call(lambda _: httpx.Response(200, json=body))

    assert error is not None
    assert error.usage == TokenUsage(prompt_tokens=5, completion_tokens=256)


def _with(**changes: object) -> dict[str, object]:
    return {**completion_body("x"), **changes}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json", headers={"content-type": "application/json"}),
        httpx.Response(200, text=json.dumps(completion_body("x"))),  # text/plain
        httpx.Response(200, json=[completion_body("x")]),
        httpx.Response(200, json=_with(object="chat.completion.chunk")),
        httpx.Response(200, json=_with(model="another-alias")),
        httpx.Response(200, json=_with(choices=[])),
        httpx.Response(200, json=_with(choices=completion_body("x")["choices"] * 2)),
        httpx.Response(200, json=_with(usage={"prompt_tokens": -1, "completion_tokens": 1})),
        httpx.Response(200, json=_with(usage={"prompt_tokens": "1", "completion_tokens": 1})),
        httpx.Response(
            200,
            json=_with(
                choices=[
                    {
                        "index": 0,
                        "message": {"role": "user", "content": "x"},
                        "finish_reason": "stop",
                    }
                ]
            ),
        ),
    ],
)
async def test_rejects_malformed_completion_bodies(response: httpx.Response) -> None:
    _, error = await call(lambda _: response)

    assert error is not None
    assert error.code is GatewayErrorCode.MALFORMED_RESPONSE


async def test_rejects_a_declared_oversized_response() -> None:
    body = json.dumps(completion_body("x" * 10_000)).encode()

    _, error = await call(
        lambda _: httpx.Response(200, content=body, headers={"content-type": "application/json"}),
        max_response_bytes=4096,
    )

    assert error is not None
    assert error.code is GatewayErrorCode.RESPONSE_TOO_LARGE


async def test_rejects_an_oversized_chunked_response() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"x" * 1024

    _, error = await call(
        lambda _: httpx.Response(
            200, content=chunks(), headers={"content-type": "application/json"}
        ),
        max_response_bytes=4096,
    )

    assert error is not None
    assert error.code is GatewayErrorCode.RESPONSE_TOO_LARGE


async def test_refuses_to_send_an_oversized_request() -> None:
    stub = GatewayStub(lambda _: httpx.Response(200, json=completion_body("x")))
    client = make_client(stub, max_request_bytes=4096)
    big = (ChatMessage(role="user", content="x" * 5000),)

    with pytest.raises(GatewayCallError) as caught:
        await client.complete(model="planner", messages=big, max_tokens=1)
    await client.aclose()

    assert caught.value.code is GatewayErrorCode.REQUEST_TOO_LARGE
    assert caught.value.may_have_executed is False
    assert stub.requests == []


async def test_close_releases_the_connection_pool() -> None:
    client = make_client(GatewayStub(lambda _: httpx.Response(200, json=completion_body("x"))))

    await client.aclose()

    assert client.is_closed


async def test_logs_neither_the_key_nor_the_prompt_nor_the_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    await call(lambda _: httpx.Response(200, json=completion_body("model output text")))
    await call(lambda _: gateway_error(401, "authentication_error", "invalid_api_key"))

    logged = " ".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)
    assert "gateway call finished" in logged
    for secret in (GATEWAY_API_KEY, "secret incident", "model output text"):
        assert secret not in logged


async def test_an_oversized_refusal_did_not_execute() -> None:
    _, error = await call(
        lambda _: httpx.Response(429, content=b"x" * 10_000), max_response_bytes=4096
    )

    assert error is not None
    assert error.code is GatewayErrorCode.RESPONSE_TOO_LARGE
    assert error.may_have_executed is False


async def test_asks_for_an_uncompressed_response() -> None:
    stub, _ = await call(lambda _: httpx.Response(200, json=completion_body("x")))

    assert stub.requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity, gzip"])
async def test_rejects_compressed_responses_before_decoding(encoding: str) -> None:
    # 10 MiB of padding compresses to about 10 KiB; the limit must apply to what
    # would be held in memory, not to the bytes on the wire.
    raw = json.dumps({**completion_body("x"), "padding": " " * (10 << 20)}).encode()
    body = gzip.compress(raw) if encoding != "deflate" else zlib.compress(raw)
    assert len(body) < 65_536

    _, error = await call(
        lambda _: httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json", "content-encoding": encoding},
        )
    )

    assert error is not None
    assert error.code is GatewayErrorCode.MALFORMED_RESPONSE
