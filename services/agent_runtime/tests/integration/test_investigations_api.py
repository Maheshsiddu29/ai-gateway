"""POST /v1/investigations through the application, with the gateway simulated by an
httpx transport and the local diagnostics reading fake procfs and cgroup trees."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_runtime.main import build_app
from tests.fakes import (
    AGENT_API_KEY,
    AUTH_HEADERS,
    GATEWAY_API_KEY,
    GatewayStub,
    completion_body,
    make_settings,
    serve,
)

PLAN = {
    "steps": [
        {"tool": "memory_info", "arguments": {}, "reason": "System memory."},
        {"tool": "cgroup_memory", "arguments": {}, "reason": "Container limit."},
        {"tool": "filesystem_capacity", "arguments": {"path": "/"}, "reason": "Disk."},
    ]
}
DIAGNOSIS = {
    "conclusion": "supported",
    "summary": "The container is at its memory limit.",
    "findings": [{"claim": "Usage equals the limit.", "evidence": ["E2"]}],
    "next_steps": ["Review the memory limit."],
}


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    procfs, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (procfs / "self").mkdir(parents=True)
    cgroup.mkdir()
    (procfs / "meminfo").write_text("MemTotal: 8000000 kB\nMemFree: 100000 kB\n")
    (procfs / "self" / "cgroup").write_text("0::/\n")
    (cgroup / "cgroup.controllers").write_text("memory\n")
    (cgroup / "memory.current").write_text("1073741824\n")
    (cgroup / "memory.max").write_text("1073741824\n")
    (cgroup / "memory.events").write_text("max 40\noom 2\noom_kill 2\n")
    return {"procfs_root": procfs, "cgroup_root": cgroup}


def scripted(plan: object = PLAN, diagnosis: object = DIAGNOSIS) -> GatewayStub:
    replies = {"planner": plan, "diagnosis": diagnosis}

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json=completion_body(json.dumps(replies[model]), model=model))

    return GatewayStub(handler)


async def post(
    client: httpx.AsyncClient, body: object, headers: dict[str, str] | None = None
) -> httpx.Response:
    return await client.post(
        "/v1/investigations", json=body, headers=AUTH_HEADERS if headers is None else headers
    )


async def test_runs_an_investigation_against_local_diagnostics(host: dict[str, Path]) -> None:
    stub = scripted()
    app = build_app(make_settings(**host), stub)

    async with serve(app) as client:
        response = await post(client, {"incident": "The service was OOM killed."})

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    assert result["investigation_id"].startswith("inv-")
    assert [(e["id"], e["tool"], e["status"]) for e in result["evidence"]] == [
        ("E1", "memory_info", "ok"),
        ("E2", "cgroup_memory", "ok"),
        ("E3", "filesystem_capacity", "ok"),
    ]
    assert result["evidence"][1]["data"]["limit_bytes"] == 1_073_741_824
    assert result["diagnosis"]["conclusion"] == "supported"
    assert result["usage"]["llm_calls"] == 2
    # The gateway sees the runtime's gateway key, never the caller's agent key.
    assert {r.headers["authorization"] for r in stub.requests} == {f"Bearer {GATEWAY_API_KEY}"}
    request_ids = [r.headers["x-request-id"] for r in stub.requests]
    assert request_ids == [f"{result['investigation_id']}-llm{n}" for n in (1, 2)]


async def test_a_failed_investigation_is_a_result_not_an_error(host: dict[str, Path]) -> None:
    plan = {"steps": [{"tool": "exec", "arguments": {"cmd": "id"}, "reason": "x"}]}
    app = build_app(make_settings(**host), scripted(plan=plan))

    async with serve(app) as client:
        response = await post(client, {"incident": "Disk is full."})

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure"] == {
        "stage": "planning",
        "code": "unknown_tool",
        "message": "The plan names a tool that does not exist.",
        "gateway_status": None,
        "gateway_error_code": None,
    }
    assert response.json()["evidence"] == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {GATEWAY_API_KEY}"}],
    ids=["missing", "wrong", "gateway-key"],
)
async def test_requires_an_agent_key(headers: dict[str, str]) -> None:
    stub = scripted()
    async with serve(build_app(make_settings(), stub)) as client:
        response = await post(client, {"incident": "x"}, headers)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert stub.requests == []


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b'{"incident": ', 400, "invalid_json"),
        (b'{"incident": "x", "tools": ["shell"]}', 400, "unknown_parameter"),
        (b'{"incident": ""}', 400, "invalid_parameter"),
        (b'{"incident": 5}', 400, "invalid_parameter"),
        (json.dumps({"incident": "x" * 4001}).encode(), 400, "invalid_parameter"),
        (b"[]", 400, "invalid_parameter"),
    ],
)
async def test_rejects_invalid_requests(body: bytes, status: int, code: str) -> None:
    stub = scripted()
    async with serve(build_app(make_settings(), stub)) as client:
        response = await client.post(
            "/v1/investigations",
            content=body,
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert stub.requests == []


async def test_rejects_other_media_types() -> None:
    async with serve(build_app(make_settings(), scripted())) as client:
        response = await client.post(
            "/v1/investigations", content=b"incident=x", headers=AUTH_HEADERS
        )

    assert response.status_code == 415


async def test_rejects_oversized_bodies_with_and_without_a_length() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b" " * 1024

    headers = {**AUTH_HEADERS, "Content-Type": "application/json"}
    async with serve(build_app(make_settings(max_request_bytes=4096), scripted())) as client:
        declared = await client.post("/v1/investigations", content=b" " * 5000, headers=headers)
        chunked = await client.post("/v1/investigations", content=chunks(), headers=headers)

    assert declared.status_code == chunked.status_code == 413


async def test_refuses_investigations_beyond_the_concurrency_limit(
    host: dict[str, Path],
) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    replies: dict[str, Any] = {"planner": PLAN, "diagnosis": DIAGNOSIS}

    class BlockingGateway(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            entered.set()
            await release.wait()
            model = json.loads(request.content)["model"]
            return httpx.Response(
                200, json=completion_body(json.dumps(replies[model]), model=model)
            )

    app = build_app(make_settings(max_concurrent_investigations=1, **host), BlockingGateway())
    async with serve(app) as client:
        first = asyncio.create_task(post(client, {"incident": "first"}))
        await entered.wait()
        second = await post(client, {"incident": "second"})
        release.set()
        first_response = await first

    assert second.status_code == 429
    assert second.json()["error"]["code"] == "concurrency_limit_exceeded"
    assert first_response.json()["status"] == "completed"


async def test_not_ready_outside_the_lifespan() -> None:
    app = build_app(make_settings(), scripted())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        response = await post(client, {"incident": "x"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"


async def test_the_agent_key_never_reaches_the_gateway(host: dict[str, Path]) -> None:
    stub = scripted()
    async with serve(build_app(make_settings(**host), stub)) as client:
        await post(client, {"incident": "x"})

    for request in stub.requests:
        assert AGENT_API_KEY not in str(request.headers)
        assert AGENT_API_KEY not in request.content.decode()
