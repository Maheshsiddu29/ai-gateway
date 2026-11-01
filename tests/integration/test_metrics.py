import logging

import pytest

from gateway.main import build_app
from tests.fakes import (
    AUTH_HEADERS,
    GATEWAY_API_KEY,
    METRICS_API_KEY,
    METRICS_HEADERS,
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
        response = await client.get("/metrics", headers=METRICS_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert 'gateway_http_requests_total{route="/v1/chat/completions",status="200"} 1.0' in text
    assert 'gateway_http_requests_total{route="unmatched",status="404"} 1.0' in text
    assert "wp-admin" not in text
    assert GATEWAY_API_KEY not in text
    assert METRICS_API_KEY not in text
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
        text = (await client.get("/metrics", headers=METRICS_HEADERS)).text

    assert 'route="/health/live"' not in text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer invalid"}, {"Authorization": f"Basic {METRICS_API_KEY}"}],
    ids=["missing", "invalid", "wrong-scheme"],
)
async def test_metrics_requires_valid_credentials(headers: dict[str, str]) -> None:
    app = build_app(make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider())
    async with serve(app) as client:
        response = await client.get("/metrics", headers=headers)
    assert response.status_code == 401
    assert "gateway_http_requests_total" not in response.text


async def test_gateway_api_keys_cannot_read_metrics() -> None:
    app = build_app(make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider())
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}

    async with serve(app) as client:
        inference = await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)
        metrics = await client.get("/metrics", headers=AUTH_HEADERS)
        # The metrics credential is not an inference credential either.
        refused = await client.post("/v1/chat/completions", json=body, headers=METRICS_HEADERS)

    assert inference.status_code == 200
    assert metrics.status_code == 401
    assert "gateway_" not in metrics.text
    assert refused.status_code == 401


async def test_metrics_refuse_everyone_without_a_metrics_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gateway.main")
    app = build_app(
        make_settings(metrics_api_keys=()),
        make_config(CONFIG, {"fake": None}),
        lambda *_: FakeProvider(),
    )

    async with serve(app) as client:
        responses = [
            await client.get("/metrics", headers=headers)
            for headers in ({}, AUTH_HEADERS, METRICS_HEADERS)
        ]

    assert [response.status_code for response in responses] == [401, 401, 401]
    assert "GATEWAY_METRICS_API_KEYS is not set" in caplog.text


async def test_metrics_credentials_never_reach_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_app(make_settings(), make_config(CONFIG, {"fake": None}), lambda *_: FakeProvider())

    async with serve(app) as client:
        await client.get("/metrics", headers=METRICS_HEADERS)
        await client.get("/metrics", headers={"Authorization": "Bearer wrong-metrics-key"})

    logged = caplog.text + " ".join(str(vars(record)) for record in caplog.records)
    assert METRICS_API_KEY not in logged
    assert "wrong-metrics-key" not in logged
    # Access is attributed to the credential's non-secret fingerprint.
    assert "metrics-" in logged
