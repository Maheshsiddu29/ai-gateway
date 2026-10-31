import pytest

from gateway.main import build_app
from tests.fakes import (
    AUTH_HEADERS,
    GATEWAY_API_KEY,
    FakeProvider,
    make_config,
    make_settings,
    serve,
)

CONFIG: dict[str, object] = {
    "providers": {"fake": {"type": "openai_compatible", "base_url": "http://fake.invalid/v1"}},
    "models": {"fast": {"provider": "fake", "model": "m"}},
}


async def test_http_metrics_use_route_templates_and_no_identifiers() -> None:
    app = build_app(make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider())
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}

    async with serve(app) as client:
        await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)
        await client.get("/scan/wp-admin.php")
        response = await client.get("/metrics", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert 'gateway_http_requests_total{route="/v1/chat/completions",status="200"} 1.0' in text
    assert 'gateway_http_requests_total{route="unmatched",status="404"} 1.0' in text
    assert "wp-admin" not in text
    assert GATEWAY_API_KEY not in text
    assert "key-" not in text


async def test_each_application_has_its_own_registry() -> None:
    first = build_app(
        make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider()
    )
    second = build_app(
        make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider()
    )

    async with serve(first) as client:
        await client.get("/health/live")
    async with serve(second) as client:
        text = (await client.get("/metrics", headers=AUTH_HEADERS)).text

    assert 'route="/health/live"' not in text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}])
async def test_metrics_requires_valid_credentials(headers: dict[str, str]) -> None:
    app = build_app(make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider())
    async with serve(app) as client:
        response = await client.get("/metrics", headers=headers)
    assert response.status_code == 401
    assert "gateway_http_requests_total" not in response.text
