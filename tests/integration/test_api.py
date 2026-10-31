"""HTTP contract tests: authentication, validation, errors, and correlation IDs."""

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from gateway.errors import ProviderRateLimitError
from gateway.main import build_app
from gateway.providers.base import Completion, Usage
from tests.fakes import AUTH_HEADERS, FakeProvider, make_config, make_settings, serve

MAX_REQUEST_BYTES = 4096
FAKE_CONFIG: dict[str, object] = {
    "providers": {"fake": {"type": "openai_compatible", "base_url": "http://fake.invalid/v1"}},
    "models": {"fast": {"provider": "fake", "model": "upstream-model"}},
}
VALID_BODY = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
async def client(provider: FakeProvider) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(
        make_settings(max_request_bytes=MAX_REQUEST_BYTES),
        make_config(FAKE_CONFIG, {"fake": None}),
        {"fake": provider},
    )
    async with serve(app) as client:
        yield client


async def post(
    client: httpx.AsyncClient, body: object, headers: dict[str, str] | None = None
) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions", json=body, headers=AUTH_HEADERS if headers is None else headers
    )


def error_of(response: httpx.Response) -> dict[str, object]:
    error: dict[str, object] = response.json()["error"]
    return error


class TestAuthentication:
    async def test_missing_credentials(self, client: httpx.AsyncClient) -> None:
        response = await post(client, VALID_BODY, headers={})

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert error_of(response)["type"] == "authentication_error"
        assert error_of(response)["code"] == "missing_api_key"

    @pytest.mark.parametrize(
        "authorization", ["Bearer wrong-key", "Basic dGVzdA==", "test-gateway-key-0123456789abcdef"]
    )
    async def test_invalid_credentials(self, client: httpx.AsyncClient, authorization: str) -> None:
        response = await post(client, VALID_BODY, headers={"Authorization": authorization})

        assert response.status_code == 401
        assert error_of(response)["type"] == "authentication_error"

    async def test_authentication_is_checked_before_the_body_is_parsed(
        self, client: httpx.AsyncClient, provider: FakeProvider
    ) -> None:
        response = await client.post(
            "/v1/chat/completions", content=b"not json", headers={"Content-Type": "text/plain"}
        )

        assert response.status_code == 401
        assert provider.requests == []

    async def test_health_endpoints_do_not_require_credentials(
        self, client: httpx.AsyncClient
    ) -> None:
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/health/ready")).json() == {"status": "ready"}


class TestCompletionResponse:
    async def test_returns_openai_compatible_body(self, client: httpx.AsyncClient) -> None:
        response = await post(client, VALID_BODY)

        assert response.status_code == 200
        body = response.json()
        assert body["id"].startswith("chatcmpl-")
        assert body["object"] == "chat.completion"
        assert body["model"] == "fast"
        assert isinstance(body["created"], int)
        assert body["choices"] == [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ]
        assert "usage" not in body

    async def test_includes_usage_reported_by_the_provider(self) -> None:
        provider = FakeProvider(
            completion=Completion(
                content="hi",
                finish_reason="length",
                usage=Usage(prompt_tokens=4, completion_tokens=1, total_tokens=5),
            )
        )
        app = build_app(
            make_settings(), make_config(FAKE_CONFIG, {"fake": None}), {"fake": provider}
        )

        async with serve(app) as client:
            body = (await post(client, VALID_BODY)).json()

        assert body["usage"] == {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}
        assert body["choices"][0]["finish_reason"] == "length"

    async def test_unknown_model_alias(self, client: httpx.AsyncClient) -> None:
        response = await post(client, {**VALID_BODY, "model": "missing"})

        assert response.status_code == 404
        assert error_of(response) | {"request_id": None} == {
            "type": "invalid_request",
            "code": "model_not_found",
            "message": "The requested model is not configured.",
            "param": "model",
            "request_id": None,
        }

    async def test_provider_errors_keep_their_status_and_retry_hint(self) -> None:
        provider = FakeProvider(
            error=ProviderRateLimitError("Rate limited.", retry_after_seconds=7)
        )
        app = build_app(
            make_settings(), make_config(FAKE_CONFIG, {"fake": None}), {"fake": provider}
        )

        async with serve(app) as client:
            response = await post(client, VALID_BODY)

        assert response.status_code == 429
        assert response.headers["retry-after"] == "7"
        assert error_of(response)["type"] == "provider_rate_limited"

    async def test_unexpected_errors_are_sanitized(self, caplog: pytest.LogCaptureFixture) -> None:
        provider = FakeProvider(error=RuntimeError("internal detail"))
        app = build_app(
            make_settings(), make_config(FAKE_CONFIG, {"fake": None}), {"fake": provider}
        )

        async with serve(app) as client:
            response = await post(client, VALID_BODY)

        assert response.status_code == 500
        assert error_of(response)["type"] == "internal_error"
        assert "internal detail" not in response.text
        assert error_of(response)["request_id"] == response.headers["x-request-id"]
        assert "internal detail" in caplog.text


class TestRequestValidation:
    @pytest.mark.parametrize(
        ("body", "error_type", "code", "param"),
        [
            ({**VALID_BODY, "temprature": 1}, "invalid_request", "unknown_parameter", "temprature"),
            (
                {**VALID_BODY, "tools": []},
                "unsupported_capability",
                "unsupported_parameter",
                "tools",
            ),
            ({**VALID_BODY, "n": 2}, "unsupported_capability", "unsupported_parameter", "n"),
            (
                {**VALID_BODY, "messages": [{"role": "user", "content": "x", "name": "bob"}]},
                "unsupported_capability",
                "unsupported_parameter",
                "messages.0.name",
            ),
            (
                {**VALID_BODY, "messages": [{"role": "tool", "content": "x"}]},
                "invalid_request",
                "invalid_parameter",
                "messages.0.role",
            ),
            (
                {**VALID_BODY, "stream_options": {"include_usage": True}},
                "invalid_request",
                "invalid_parameter",
                "stream_options",
            ),
            (
                {**VALID_BODY, "temperature": 3},
                "invalid_request",
                "invalid_parameter",
                "temperature",
            ),
            ({"model": "fast"}, "invalid_request", "invalid_parameter", "messages"),
            ([VALID_BODY], "invalid_request", "invalid_parameter", None),
        ],
    )
    async def test_rejected_bodies(
        self,
        client: httpx.AsyncClient,
        provider: FakeProvider,
        body: object,
        error_type: str,
        code: str,
        param: str | None,
    ) -> None:
        response = await post(client, body)

        assert response.status_code == 400
        error = error_of(response)
        assert (error["type"], error["code"], error["param"]) == (error_type, code, param)
        assert provider.requests == []

    async def test_invalid_json(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/chat/completions",
            content=b'{"model": ',
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert error_of(response)["code"] == "invalid_json"

    async def test_requires_json_content_type(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/chat/completions",
            content=json.dumps(VALID_BODY).encode(),
            headers={**AUTH_HEADERS, "Content-Type": "text/plain"},
        )

        assert response.status_code == 415

    async def test_validation_errors_do_not_echo_input(self, client: httpx.AsyncClient) -> None:
        response = await post(
            client, {**VALID_BODY, "messages": [{"role": "secret-role-value", "content": "x"}]}
        )

        assert response.status_code == 400
        assert "secret-role-value" not in response.text


class TestRequestSizeLimit:
    def body_of_size(self, size: int) -> bytes:
        def encode(content: str) -> bytes:
            message = {"role": "user", "content": content}
            return json.dumps({"model": "fast", "messages": [message]}).encode()

        encoded = encode("x" * (size - len(encode(""))))
        assert len(encoded) == size
        return encoded

    async def send(
        self, client: httpx.AsyncClient, content: bytes | AsyncIterator[bytes]
    ) -> httpx.Response:
        return await client.post(
            "/v1/chat/completions",
            content=content,
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )

    async def test_body_at_the_limit_is_accepted(self, client: httpx.AsyncClient) -> None:
        response = await self.send(client, self.body_of_size(MAX_REQUEST_BYTES))

        assert response.status_code == 200

    async def test_declared_length_over_the_limit_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await self.send(client, self.body_of_size(MAX_REQUEST_BYTES + 1))

        assert response.status_code == 413
        assert error_of(response)["code"] == "request_too_large"

    async def test_chunked_body_over_the_limit_is_rejected(self, client: httpx.AsyncClient) -> None:
        payload = self.body_of_size(MAX_REQUEST_BYTES + 1)

        async def chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(payload), 1000):
                yield payload[start : start + 1000]

        response = await self.send(client, chunks())

        assert response.status_code == 413


class TestRequestCorrelation:
    async def test_generates_request_id(self, client: httpx.AsyncClient) -> None:
        response = await post(client, VALID_BODY, headers={})

        request_id = response.headers["x-request-id"]
        assert len(request_id) == 32
        assert error_of(response)["request_id"] == request_id

    async def test_accepts_well_formed_client_request_id(self, client: httpx.AsyncClient) -> None:
        response = await post(
            client, VALID_BODY, headers={**AUTH_HEADERS, "X-Request-ID": "abc-123.DEF_456"}
        )

        assert response.headers["x-request-id"] == "abc-123.DEF_456"

    @pytest.mark.parametrize("supplied", ["short", "has space here", "x" * 129, "badéchars-123"])
    async def test_replaces_malformed_client_request_id(
        self, client: httpx.AsyncClient, supplied: str
    ) -> None:
        response = await client.get("/health/live", headers={b"X-Request-ID": supplied.encode()})

        assert response.headers["x-request-id"] != supplied
        assert len(response.headers["x-request-id"]) == 32

    async def test_access_log_records_request_outcome(
        self, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        await post(client, VALID_BODY, headers={})

        [record] = [r for r in caplog.records if r.getMessage() == "request completed"]
        assert record.__dict__["status_code"] == 401
        assert record.__dict__["error_type"] == "authentication_error"
        assert record.__dict__["path"] == "/v1/chat/completions"
        assert "duration_ms" in record.__dict__


class TestRoutingErrors:
    async def test_unknown_path(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/unknown")

        assert response.status_code == 404
        assert error_of(response)["code"] == "not_found"

    async def test_wrong_method(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/chat/completions")

        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert error_of(response)["code"] == "method_not_allowed"


class TestLifecycle:
    async def test_not_ready_before_startup(self) -> None:
        app = build_app(
            make_settings(), make_config(FAKE_CONFIG, {"fake": None}), {"fake": FakeProvider()}
        )
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            response = await client.get("/health/ready")

        assert response.status_code == 503

    async def test_shutdown_closes_providers(self) -> None:
        provider = FakeProvider()
        app = build_app(
            make_settings(), make_config(FAKE_CONFIG, {"fake": None}), {"fake": provider}
        )

        async with serve(app):
            assert not provider.closed

        assert provider.closed
