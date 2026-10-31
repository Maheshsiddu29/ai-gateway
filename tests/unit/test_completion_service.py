import asyncio
import logging

import pytest

from gateway.core.security import Principal
from gateway.errors import InvalidRequestError, ProviderRateLimitError
from gateway.providers.base import Completion, Usage
from gateway.schemas.completions import ChatCompletionRequest, ChatMessage
from gateway.services.completion import CompletionService, ModelRoute
from tests.fakes import FakeProvider

PRINCIPAL = Principal(id="key-abc")


def make_service(
    provider: FakeProvider, default_max_tokens: int | None = None
) -> CompletionService:
    routes = {
        "fast": ModelRoute(
            alias="fast",
            provider=provider.name,
            upstream_model="upstream-model",
            default_max_tokens=default_max_tokens,
        )
    }
    return CompletionService(routes, {provider.name: provider})


def make_request(**fields: object) -> ChatCompletionRequest:
    values: dict[str, object] = {
        "model": "fast",
        "messages": [ChatMessage(role="user", content="secret prompt")],
    }
    values.update(fields)
    return ChatCompletionRequest.model_validate(values)


async def test_resolves_alias_to_upstream_model() -> None:
    provider = FakeProvider()

    await make_service(provider).complete(make_request(temperature=0.3, stop=["x"]), PRINCIPAL)

    [sent] = provider.requests
    assert sent.model == "upstream-model"
    assert sent.temperature == 0.3
    assert sent.stop == ("x",)


async def test_unknown_alias_is_rejected_without_calling_a_provider() -> None:
    provider = FakeProvider()

    with pytest.raises(InvalidRequestError) as exc_info:
        await make_service(provider).complete(make_request(model="other"), PRINCIPAL)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "model_not_found"
    assert provider.requests == []


@pytest.mark.parametrize(("requested", "expected"), [(None, 256), (12, 12)])
async def test_default_max_tokens_applies_only_when_client_omits_it(
    requested: int | None, expected: int
) -> None:
    provider = FakeProvider()

    await make_service(provider, default_max_tokens=256).complete(
        make_request(max_tokens=requested), PRINCIPAL
    )

    assert provider.requests[0].max_tokens == expected


def test_routes_must_reference_known_providers() -> None:
    route = ModelRoute(alias="a", provider="missing", upstream_model="m", default_max_tokens=None)

    with pytest.raises(ValueError, match="missing"):
        CompletionService({"a": route}, {})


async def test_logs_success_with_usage_but_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    completion = Completion(
        content="secret answer",
        finish_reason="stop",
        usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        provider_response_id="resp-1",
    )
    caplog.set_level(logging.INFO)

    await make_service(FakeProvider(completion=completion)).complete(make_request(), PRINCIPAL)

    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    assert record.__dict__["outcome"] == "success"
    assert record.__dict__["model_alias"] == "fast"
    assert record.__dict__["provider"] == "fake"
    assert record.__dict__["principal_id"] == "key-abc"
    assert record.__dict__["total_tokens"] == 5
    assert "secret" not in caplog.text


async def test_logs_provider_errors(caplog: pytest.LogCaptureFixture) -> None:
    error = ProviderRateLimitError("Rate limited.", upstream_status=429)
    caplog.set_level(logging.INFO)

    with pytest.raises(ProviderRateLimitError):
        await make_service(FakeProvider(error=error)).complete(make_request(), PRINCIPAL)

    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    assert record.__dict__["outcome"] == "error"
    assert record.__dict__["error_type"] == "provider_rate_limited"
    assert record.__dict__["upstream_status"] == 429


async def test_logs_cancellation(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    with pytest.raises(asyncio.CancelledError):
        await make_service(FakeProvider(error=asyncio.CancelledError())).complete(
            make_request(), PRINCIPAL
        )

    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    assert record.__dict__["outcome"] == "cancelled"
