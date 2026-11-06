"""Offline demonstration of one investigation: agent runtime -> gateway -> mock provider.

Everything runs in this process over ASGI transports: no network, no provider
account, no Redis or PostgreSQL. The gateway uses config/gateway.mock.toml, whose
agent aliases return a scripted plan and diagnosis. The diagnostics read this
machine, so on macOS the procfs and cgroup diagnostics are reported unavailable.

    uv run python tools/agent_demo.py "The API container restarted with exit code 137."
"""

import argparse
import asyncio
import json
import secrets
from functools import partial
from pathlib import Path

import httpx
from agent_runtime.config import Settings as AgentSettings
from agent_runtime.main import build_app as build_agent_app

from gateway.core.config import Settings as GatewaySettings
from gateway.core.config import load_config
from gateway.main import build_app as build_gateway_app
from gateway.providers.factory import build_provider
from mock_upstream import create_app as create_mock_app

MOCK_CONFIG = Path(__file__).parents[1] / "config" / "gateway.mock.toml"


async def demo(incident: str) -> dict[str, object]:
    # Keys exist only in this process.
    gateway_key, agent_key = secrets.token_hex(32), secrets.token_hex(32)
    gateway_settings = GatewaySettings.model_validate(
        {"api_keys": (gateway_key,), "environment": "development", "config_file": MOCK_CONFIG}
    )
    gateway = build_gateway_app(
        gateway_settings,
        load_config(gateway_settings, {"MOCK_UPSTREAM_API_KEY": "mock-upstream-key"}),
        partial(
            build_provider,
            transport=httpx.ASGITransport(app=create_mock_app(stream_delay_seconds=0)),
        ),
    )
    agent_settings = AgentSettings.model_validate(
        {
            "environment": "development",
            "api_keys": (agent_key,),
            "gateway_url": "http://gateway.local",
            "gateway_api_key": gateway_key,
            "planner_model": "agent-planner",
            "diagnosis_model": "agent-diagnosis",
        }
    )
    agent = build_agent_app(agent_settings, httpx.ASGITransport(app=gateway))
    async with (
        gateway.router.lifespan_context(gateway),
        agent.router.lifespan_context(agent),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent), base_url="http://agent.local"
        ) as client,
    ):
        response = await client.post(
            "/v1/investigations",
            json={"incident": incident},
            headers={"Authorization": f"Bearer {agent_key}"},
        )
    response.raise_for_status()
    result: dict[str, object] = response.json()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("incident", help="incident description, at most 4000 characters")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(demo(args.incident)), indent=2))


if __name__ == "__main__":
    main()
