"""The agent runtime against the real gateway application.

The runtime's gateway client talks to the gateway through an ASGI transport, and
the gateway talks to the mock provider or an upstream stub, so these tests check
the HTTP contract between the two services: the request fields the gateway's
strict schema accepts, its error envelope and statuses, and its retry behaviour.
Diagnostics read fake procfs and cgroup trees, plus statvfs on "/".
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_runtime.config import ApiSettings, WorkerSettings
from agent_runtime.diagnostics.local import LocalDiagnostics
from agent_runtime.durable.worker import Worker, WorkerConfig
from agent_runtime.factories import build_diagnostic_executor, build_gateway_client
from agent_runtime.gateway_client import ChatMessage, GatewayClient, GatewayTimeouts
from agent_runtime.investigation.graph import ModelAliases
from agent_runtime.investigation.workflow import (
    InvestigationLimits,
    Investigator,
    new_investigation_id,
)
from agent_runtime.main import build_app as build_agent_app
from agent_runtime.persistence import migrations
from agent_runtime.persistence.checkpoints import checkpointer
from agent_runtime.persistence.database import connect, create_pool
from agent_runtime.persistence.store import InvestigationStore
from fastapi import FastAPI
from pydantic import SecretStr

from gateway.core.config import Environment, load_config
from gateway.main import build_app as build_gateway_app
from gateway.providers.factory import build_provider
from mock_upstream import create_app as create_mock_app
from tests.fakes import (
    GATEWAY_API_KEY,
    UpstreamHandler,
    UpstreamStub,
    make_config,
    make_settings,
)
from tests.redis_server import RedisServer

MOCK_CONFIG = Path(__file__).parents[2] / "config" / "gateway.mock.toml"
AGENT_API_KEY = "test-agent-runtime-key-0123456789abcdef"
AGENT_HEADERS = {"Authorization": f"Bearer {AGENT_API_KEY}"}
INCIDENT = "The payments container restarted with exit code 137."


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.models: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.models.append(json.loads(request.content)["model"])
        return await self._inner.handle_async_request(request)


def mock_gateway() -> tuple[FastAPI, CountingTransport]:
    upstream = CountingTransport(httpx.ASGITransport(app=create_mock_app(stream_delay_seconds=0)))
    settings = make_settings(config_file=MOCK_CONFIG, environment=Environment.DEVELOPMENT)
    config = load_config(settings, {"MOCK_UPSTREAM_API_KEY": "mock-upstream-key"})
    return build_gateway_app(
        settings, config, partial(build_provider, transport=upstream)
    ), upstream


def stub_gateway(
    handler: UpstreamHandler, *, redis_url: str | None = None, limits: dict[str, int] | None = None
) -> tuple[FastAPI, UpstreamStub]:
    stub = UpstreamStub(handler)
    raw: dict[str, object] = {
        "providers": {"openai": {"type": "openai_compatible", "base_url": "https://llm.test/v1"}},
        "models": {
            alias: {"provider": "openai", "model": f"upstream-{alias}", "max_output_tokens": 2048}
            for alias in ("agent-planner", "agent-diagnosis")
        },
    }
    if limits is not None:
        raw["admission"] = {"enabled": True, "default_limits": limits}
    app = build_gateway_app(
        make_settings(redis_url=redis_url),
        make_config(raw, {"openai": None}),
        partial(build_provider, transport=stub),
    )
    return app, stub


@dataclass(frozen=True)
class AgentConfig:
    settings: WorkerSettings
    max_output_tokens: int


def agent_settings(host: dict[str, Path], **overrides: Any) -> AgentConfig:
    max_output_tokens = overrides.pop("max_output_tokens", 2048)
    settings = WorkerSettings.model_validate(
        {
            "environment": "development",
            "gateway_url": "http://gateway.test",
            "gateway_api_key": GATEWAY_API_KEY,
            "planner_model": "agent-planner",
            "diagnosis_model": "agent-diagnosis",
            "database_url": "postgresql://unused:unused@localhost/unused",
            **host,
            **overrides,
        }
    )
    return AgentConfig(settings, max_output_tokens)


type Run = Callable[[], Awaitable[dict[str, Any]]]


@asynccontextmanager
async def agent(gateway: FastAPI, config: AgentConfig) -> AsyncIterator[Run]:
    """Run the investigation graph in memory against the real gateway application.

    This exercises the runtime's gateway client, prompts, and validators over the
    gateway's HTTP contract; the durable path is covered by
    test_durable_investigation_through_the_gateway below.
    """
    settings = config.settings
    client = build_gateway_client(settings, httpx.ASGITransport(app=gateway))
    local = LocalDiagnostics(settings.procfs_root, settings.cgroup_root)
    investigator = Investigator(
        client,
        build_diagnostic_executor(settings, local),
        models=ModelAliases(settings.planner_model, settings.diagnosis_model),
        limits=InvestigationLimits(
            max_llm_calls=2,
            max_tool_calls=6,
            max_output_tokens=config.max_output_tokens,
            timeout_seconds=60,
        ),
        allowed_paths=frozenset(settings.filesystem_paths),
    )

    async def run() -> dict[str, Any]:
        result = await investigator.investigate(INCIDENT, investigation_id=new_investigation_id())
        return result.model_dump(mode="json")

    try:
        async with gateway.router.lifespan_context(gateway):
            yield run
    finally:
        await client.aclose()
        local.close()


async def investigate(run: Run) -> dict[str, Any]:
    return await run()


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    procfs, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (procfs / "self").mkdir(parents=True)
    cgroup.mkdir()
    (procfs / "meminfo").write_text("MemTotal: 2048000 kB\nMemFree: 10240 kB\n")
    (procfs / "self" / "cgroup").write_text("0::/\n")
    (cgroup / "cgroup.controllers").write_text("memory\n")
    (cgroup / "memory.current").write_text("536870912\n")
    (cgroup / "memory.max").write_text("536870912\n")
    (cgroup / "memory.events").write_text("max 9\noom 1\noom_kill 1\n")
    return {"procfs_root": procfs, "cgroup_root": cgroup}


def openai_reply(content: str | None, finish_reason: str | None = "stop") -> httpx.Response:
    choice = {"index": 0, "message": {"role": "assistant", "content": content}}
    return httpx.Response(
        200,
        json={
            "id": "upstream-1",
            "choices": [{**choice, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
        },
    )


async def test_the_gateway_accepts_the_runtime_request_contract() -> None:
    gateway, upstream = mock_gateway()
    client = GatewayClient(
        "http://gateway.test",
        SecretStr(GATEWAY_API_KEY),
        timeouts=GatewayTimeouts(),
        max_connections=1,
        max_request_bytes=65_536,
        max_response_bytes=65_536,
        transport=httpx.ASGITransport(app=gateway),
    )

    async with gateway.router.lifespan_context(gateway):
        completion = await client.complete(
            model="fast",
            messages=[
                ChatMessage(role="system", content="Be brief."),
                ChatMessage(role="user", content="one two three"),
            ],
            max_tokens=64,
            request_id="inv-contract-llm1",
        )
    await client.aclose()

    assert completion.content == "Mock reply from mock-small: received 3 words."
    assert completion.usage is not None
    assert completion.gateway_request_id == "inv-contract-llm1"
    assert upstream.models == ["mock-small"]


async def test_investigates_through_the_gateway_and_mock_provider(host: dict[str, Path]) -> None:
    gateway, upstream = mock_gateway()

    async with agent(gateway, agent_settings(host)) as client:
        result = await investigate(client)

    assert result["status"] == "completed", result["failure"]
    assert [(e["id"], e["tool"]) for e in result["evidence"]] == [
        ("E1", "filesystem_capacity"),
        ("E2", "filesystem_inodes"),
        ("E3", "memory_info"),
        ("E4", "cgroup_memory"),
    ]
    assert result["evidence"][0]["status"] == "ok"
    assert result["evidence"][3]["data"]["oom_kill_events"] == 1
    assert result["diagnosis"]["findings"][0]["evidence"] == ["E1"]
    assert result["usage"]["llm_calls"] == 2
    assert result["usage"]["prompt_tokens"] > 0
    assert upstream.models == ["mock-agent-planner", "mock-agent-diagnosis"]


async def test_a_wrong_gateway_key_fails_before_any_provider_call(host: dict[str, Path]) -> None:
    gateway, upstream = mock_gateway()
    settings = agent_settings(host, gateway_api_key="not-a-gateway-key")

    async with agent(gateway, settings) as client:
        result = await investigate(client)

    assert result["failure"] == {
        "stage": "planning",
        "code": "gateway_authentication_failed",
        "message": "The gateway rejected the key.",
        "gateway_status": 401,
        "gateway_error_code": "invalid_api_key",
    }
    assert upstream.models == []


@pytest.mark.parametrize(
    ("overrides", "gateway_code"),
    [
        ({"max_output_tokens": 4096}, "max_tokens_exceeds_limit"),
        ({"planner_model": "no-such-alias"}, "model_not_found"),
    ],
)
async def test_gateway_rejections_are_reported_with_the_gateway_code(
    host: dict[str, Path], overrides: dict[str, object], gateway_code: str
) -> None:
    gateway, upstream = mock_gateway()

    async with agent(gateway, agent_settings(host, **overrides)) as client:
        result = await investigate(client)

    assert result["failure"]["code"] == "gateway_request_rejected"
    assert result["failure"]["gateway_error_code"] == gateway_code
    assert upstream.models == []


@pytest.mark.parametrize(
    ("handler", "code", "gateway_status"),
    [
        (
            lambda _: httpx.Response(500, json={"error": {"type": "server_error"}}),
            "gateway_unavailable",
            502,
        ),
        (
            lambda _: httpx.Response(529, json={"error": {"type": "overloaded"}}),
            "gateway_unavailable",
            502,
        ),
        (lambda _: openai_reply("{", finish_reason="length"), "completion_truncated", 200),
        (lambda _: openai_reply("", finish_reason="content_filter"), "completion_refused", 200),
        (lambda _: openai_reply("  "), "completion_empty", 200),
        # The gateway itself treats null content as a malformed provider response.
        (lambda _: openai_reply(None), "gateway_unavailable", 502),
        (lambda _: openai_reply("Let me think about it."), "malformed_json", None),
    ],
)
async def test_provider_failures_after_dispatch_are_not_retried(
    host: dict[str, Path], handler: UpstreamHandler, code: str, gateway_status: int | None
) -> None:
    gateway, stub = stub_gateway(handler)

    async with agent(gateway, agent_settings(host)) as client:
        result = await investigate(client)

    assert result["status"] == "failed"
    assert result["failure"]["stage"] == "planning"
    assert result["failure"]["code"] == code
    assert result["failure"]["gateway_status"] == gateway_status
    # Neither the gateway nor the runtime repeats a request the provider may have run.
    assert len(stub.requests) == 1
    assert result["evidence"] == []


async def test_a_provider_read_timeout_is_not_retried(host: dict[str, Path]) -> None:
    def time_out(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    gateway, stub = stub_gateway(time_out)

    async with agent(gateway, agent_settings(host)) as client:
        result = await investigate(client)

    assert result["failure"]["code"] == "gateway_timeout"
    assert result["failure"]["gateway_status"] == 504
    assert len(stub.requests) == 1


async def test_gateway_rate_limits_end_the_investigation(
    host: dict[str, Path], redis_server: RedisServer
) -> None:
    plan = {"steps": [{"tool": "memory_info", "arguments": {}, "reason": "Memory."}]}
    limits = {
        "requests_per_minute": 1,
        "request_burst": 1,
        "tokens_per_minute": 1_000_000,
        "token_burst": 100_000,
        "max_concurrent_requests": 4,
    }
    gateway, stub = stub_gateway(
        lambda _: openai_reply(json.dumps(plan)), redis_url=redis_server.url, limits=limits
    )

    async with agent(gateway, agent_settings(host)) as client:
        result = await investigate(client)

    # The plan used the key's only request; the diagnosis call is refused by admission.
    assert result["failure"]["stage"] == "diagnosis"
    assert result["failure"]["code"] == "gateway_rate_limited"
    assert result["failure"]["gateway_error_code"] == "requests_per_minute_exceeded"
    assert len(result["evidence"]) == 1
    assert len(stub.requests) == 1


@pytest.fixture
async def agent_database() -> AsyncIterator[tuple[SecretStr, str]]:
    """A migrated agent schema in AGENT_TEST_DATABASE_URL, dropped afterwards."""
    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("AGENT_REQUIRE_DATABASE_TESTS") == "1":
            pytest.fail("AGENT_TEST_DATABASE_URL is required but not set")
        pytest.skip("AGENT_TEST_DATABASE_URL is not set")
    secret, schema = SecretStr(url), f"agent_contract_{uuid.uuid4().hex[:12]}"
    async with await connect(secret, schema, "contract-tests") as conn:
        await migrations.upgrade(conn, schema)
    try:
        yield secret, schema
    finally:
        async with await connect(secret, "public", "contract-tests") as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def test_durable_investigation_through_the_gateway(
    host: dict[str, Path], agent_database: tuple[SecretStr, str]
) -> None:
    url, schema = agent_database
    gateway, upstream = mock_gateway()
    settings = agent_settings(host).settings
    pool = create_pool(url, schema, "contract-tests", max_size=4, timeout=10)
    await pool.open(wait=True)
    api = build_agent_app(
        ApiSettings.model_validate(
            {
                "environment": "development",
                "api_keys": (AGENT_API_KEY,),
                "database_url": url,
                "planner_model": "agent-planner",
                "diagnosis_model": "agent-diagnosis",
                "model_prices": {
                    alias: {
                        "input_micro_usd_per_million_tokens": 1_000_000,
                        "output_micro_usd_per_million_tokens": 2_000_000,
                    }
                    for alias in ("agent-planner", "agent-diagnosis")
                },
            }
        ),
        pool,
    )
    client = build_gateway_client(settings, httpx.ASGITransport(app=gateway))
    local = LocalDiagnostics(settings.procfs_root, settings.cgroup_root)
    worker = Worker(
        InvestigationStore(pool),
        checkpointer(pool),
        client,
        build_diagnostic_executor(settings, local),
        WorkerConfig(
            worker_id="contract-worker",
            concurrency=1,
            max_running=4,
            lease_seconds=10,
            poll_seconds=0.05,
            shutdown_grace_seconds=1,
            max_recovery_attempts=3,
            models=ModelAliases("agent-planner", "agent-diagnosis"),
            allowed_paths=frozenset({"/"}),
        ),
    )
    try:
        async with (
            gateway.router.lifespan_context(gateway),
            api.router.lifespan_context(api),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
            ) as http,
        ):
            accepted = await http.post(
                "/v1/investigations", json={"incident": INCIDENT}, headers=AGENT_HEADERS
            )
            await worker.run_until_idle()
            investigation_id = accepted.json()["investigation_id"]
            done = await http.get(f"/v1/investigations/{investigation_id}", headers=AGENT_HEADERS)
    finally:
        await client.aclose()
        local.close()
        await pool.close()

    assert accepted.status_code == 202
    body = done.json()
    assert body["status"] == "completed", body["result"]
    assert body["result"]["diagnosis"]["findings"][0]["evidence"] == ["E1"]
    assert upstream.models == ["mock-agent-planner", "mock-agent-diagnosis"]
