"""Optional smoke test against a real provider. Spends real tokens.

Skipped unless GATEWAY_LIVE_TESTS=1. Configure with:

    GATEWAY_LIVE_CONFIG_FILE  path to a gateway TOML file
    GATEWAY_LIVE_MODEL        model alias to call
    plus the provider API key variables that file references.
"""

import os
from pathlib import Path

import pytest

from gateway.core.config import Environment, load_config
from gateway.main import build_app
from tests.fakes import AUTH_HEADERS, make_settings, serve

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("GATEWAY_LIVE_TESTS") != "1", reason="set GATEWAY_LIVE_TESTS=1 to run"
    ),
]


@pytest.mark.parametrize("stream", [False, True])
async def test_live_completion(stream: bool) -> None:
    settings = make_settings(
        config_file=Path(os.environ["GATEWAY_LIVE_CONFIG_FILE"]),
        environment=Environment.DEVELOPMENT,
    )
    app = build_app(settings, load_config(settings, os.environ))
    body = {
        "model": os.environ["GATEWAY_LIVE_MODEL"],
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "max_tokens": 64,
        "stream": stream,
    }

    async with serve(app) as client:
        response = await client.post("/v1/chat/completions", json=body, headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    if stream:
        assert response.text.rstrip().endswith("data: [DONE]")
    else:
        assert response.json()["choices"][0]["message"]["content"]
