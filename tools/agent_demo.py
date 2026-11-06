"""Offline demonstration of one investigation: investigation graph -> gateway -> mock provider.

Everything runs in this process over ASGI transports: no network, no provider
account, no Redis or PostgreSQL. The gateway uses config/gateway.mock.toml, whose
agent aliases return a scripted plan and diagnosis. The graph runs in memory
(agent_runtime.investigation.workflow.Investigator), without the durable
worker's journal and checkpoints; `docker compose --profile agent up` runs the
durable service. The diagnostics read this machine, so on macOS the procfs and
cgroup diagnostics are reported unavailable.

    uv run python tools/agent_demo.py "The API container restarted with exit code 137."
"""

import argparse
import asyncio
import json
import secrets
from functools import partial
from pathlib import Path

import httpx
from agent_runtime.config import WorkerSettings
from agent_runtime.diagnostics.local import LocalDiagnostics
from agent_runtime.factories import build_diagnostic_executor, build_gateway_client
from agent_runtime.investigation.graph import ModelAliases
from agent_runtime.investigation.workflow import (
    InvestigationLimits,
    Investigator,
    new_investigation_id,
)

from gateway.core.config import Settings as GatewaySettings
from gateway.core.config import load_config
from gateway.main import build_app as build_gateway_app
from gateway.providers.factory import build_provider
from mock_upstream import create_app as create_mock_app

MOCK_CONFIG = Path(__file__).parents[1] / "config" / "gateway.mock.toml"


async def demo(incident: str) -> dict[str, object]:
    # The key exists only in this process.
    gateway_key = secrets.token_hex(32)
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
    settings = WorkerSettings.model_validate(
        {
            "environment": "development",
            "gateway_url": "http://gateway.local",
            "gateway_api_key": gateway_key,
            "planner_model": "agent-planner",
            "diagnosis_model": "agent-diagnosis",
            # Not used: the demonstration keeps everything in memory.
            "database_url": "postgresql://unused:unused@localhost/unused",
        }
    )
    client = build_gateway_client(settings, httpx.ASGITransport(app=gateway))
    local = LocalDiagnostics(settings.procfs_root, settings.cgroup_root)
    investigator = Investigator(
        client,
        build_diagnostic_executor(settings, local),
        models=ModelAliases(settings.planner_model, settings.diagnosis_model),
        limits=InvestigationLimits(
            max_llm_calls=2, max_tool_calls=6, max_output_tokens=2048, timeout_seconds=60
        ),
        allowed_paths=frozenset(settings.filesystem_paths),
    )
    try:
        async with gateway.router.lifespan_context(gateway):
            result = await investigator.investigate(
                incident, investigation_id=new_investigation_id()
            )
    finally:
        await client.aclose()
        local.close()
    return result.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("incident", help="incident description, at most 4000 characters")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(demo(args.incident)), indent=2))


if __name__ == "__main__":
    main()
