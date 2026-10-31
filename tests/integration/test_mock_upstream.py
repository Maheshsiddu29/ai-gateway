"""The local development setup: the gateway with config/gateway.mock.toml and the mock."""

import json
from functools import partial
from pathlib import Path

import httpx
import pytest

from gateway.core.config import Environment, load_config
from gateway.main import build_app
from gateway.providers.factory import build_provider
from mock_upstream import create_app as create_mock_app
from tests.fakes import AUTH_HEADERS, make_settings, serve

MOCK_CONFIG = Path(__file__).parents[2] / "config" / "gateway.mock.toml"


@pytest.mark.parametrize("alias", ["fast", "balanced", "quality"])
@pytest.mark.parametrize("stream", [False, True])
async def test_every_mock_alias_completes(alias: str, stream: bool) -> None:
    settings = make_settings(config_file=MOCK_CONFIG, environment=Environment.DEVELOPMENT)
    config = load_config(settings, {"MOCK_UPSTREAM_API_KEY": "mock-upstream-key"})
    mock_transport = httpx.ASGITransport(app=create_mock_app(stream_delay_seconds=0))
    app = build_app(settings, config, partial(build_provider, transport=mock_transport))
    body = {
        "model": alias,
        "messages": [{"role": "user", "content": "one two three"}],
        "stream": stream,
    }

    async with serve(app) as client:
        response = await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)

    assert response.status_code == 200
    if stream:
        payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1] == "[DONE]"
        text = "".join(
            json.loads(p)["choices"][0]["delta"].get("content", "") for p in payloads[:-1]
        )
    else:
        text = response.json()["choices"][0]["message"]["content"]
    assert text.endswith("received 3 words.")
