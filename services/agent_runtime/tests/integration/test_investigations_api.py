"""POST and GET /v1/investigations against PostgreSQL.

The API only stores and reads investigations; tests that need one to finish run
the worker in-process against a scripted gateway.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.types import Message

from agent_runtime.diagnostics.tools import DiagnosticExecutor
from agent_runtime.durable.worker import Worker, WorkerConfig
from agent_runtime.gateway_client import GatewayClient, GatewayTimeouts
from agent_runtime.investigation.graph import WORKFLOW_VERSION, ModelAliases
from agent_runtime.main import build_app
from agent_runtime.persistence.checkpoints import checkpointer
from agent_runtime.persistence.database import Connection, Pool
from agent_runtime.persistence.store import InvestigationStore
from tests.db import investigation
from tests.fakes import (
    AGENT_API_KEY,
    AUTH_HEADERS,
    DIAGNOSIS_REPLY,
    GATEWAY_API_KEY,
    OTHER_AUTH_HEADERS,
    PLAN_REPLY,
    CountingBackend,
    GatewayStub,
    completion_body,
    make_settings,
    serve,
)

INCIDENT = {"incident": "The payments container was OOM killed."}


async def post(
    client: httpx.AsyncClient,
    body: object = INCIDENT,
    headers: dict[str, str] | None = None,
    key: str | None = None,
) -> httpx.Response:
    sent = dict(AUTH_HEADERS if headers is None else headers)
    if key is not None:
        sent["Idempotency-Key"] = key
    return await client.post("/v1/investigations", json=body, headers=sent)


async def run_worker(pool: Pool) -> CountingBackend:
    def reply(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        content = PLAN_REPLY if model == "planner" else DIAGNOSIS_REPLY
        return httpx.Response(200, json=completion_body(content, model=model))

    backend = CountingBackend()
    client = GatewayClient(
        "http://gateway.test",
        SecretStr(GATEWAY_API_KEY),
        timeouts=GatewayTimeouts(),
        max_connections=2,
        max_request_bytes=262_144,
        max_response_bytes=65_536,
        transport=GatewayStub(reply),
    )
    worker = Worker(
        InvestigationStore(pool),
        checkpointer(pool),
        client,
        DiagnosticExecutor(backend, timeout_seconds=5, max_output_bytes=16_384),
        WorkerConfig(
            worker_id="worker-test",
            concurrency=2,
            max_running=10,
            lease_seconds=10,
            poll_seconds=0.05,
            shutdown_grace_seconds=1,
            max_recovery_attempts=3,
            models=ModelAliases("planner", "diagnosis"),
            allowed_paths=frozenset({"/"}),
        ),
    )
    await worker.run_until_idle()
    await client.aclose()
    return backend


@pytest.fixture
async def client(pool: Pool) -> AsyncIterator[httpx.AsyncClient]:
    async with serve(build_app(make_settings(), pool)) as client:
        yield client


async def test_accepts_and_persists_before_answering(
    client: httpx.AsyncClient, owner: Connection
) -> None:
    response = await post(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["result"] is None
    assert response.headers["location"] == f"/v1/investigations/{body['investigation_id']}"
    row = await investigation(owner, body["investigation_id"])
    assert row["status"] == "accepted"
    assert row["incident"] == INCIDENT["incident"]
    assert row["workflow_version"] == WORKFLOW_VERSION
    assert (row["max_llm_calls"], row["max_tool_calls"], row["max_output_tokens"]) == (2, 6, 2048)
    assert row["principal_id"].startswith("agent-key-")


async def test_a_finished_investigation_is_returned_with_its_result(
    client: httpx.AsyncClient, pool: Pool
) -> None:
    investigation_id = (await post(client)).json()["investigation_id"]

    pending = await client.get(f"/v1/investigations/{investigation_id}", headers=AUTH_HEADERS)
    await run_worker(pool)
    done = await client.get(f"/v1/investigations/{investigation_id}", headers=AUTH_HEADERS)

    assert pending.json()["status"] == "accepted"
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "completed"
    assert body["stage"] == "finalize"
    assert body["result"]["diagnosis"]["conclusion"] == "supported"
    assert [e["id"] for e in body["result"]["evidence"]] == ["E1", "E2"]
    assert "incident" not in json.dumps(body)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {GATEWAY_API_KEY}"}],
    ids=["missing", "wrong", "gateway-key"],
)
async def test_requires_an_agent_key(
    client: httpx.AsyncClient, owner: Connection, headers: dict[str, str]
) -> None:
    created = await post(client, headers=headers)
    read = await client.get(f"/v1/investigations/inv-{'0' * 32}", headers=headers)

    assert created.status_code == read.status_code == 401
    cursor = await owner.execute("SELECT count(*) AS n FROM investigations")
    assert await cursor.fetchone() == {"n": 0}


async def test_authenticates_before_reading_the_body(pool: Pool) -> None:
    async with serve(build_app(make_settings(max_request_bytes=4096), pool)) as client:
        response = await client.post(
            "/v1/investigations",
            content=b" " * 10_000,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401


async def test_other_callers_investigations_look_like_unknown_ones(
    client: httpx.AsyncClient,
) -> None:
    investigation_id = (await post(client)).json()["investigation_id"]

    theirs = await client.get(f"/v1/investigations/{investigation_id}", headers=OTHER_AUTH_HEADERS)
    unknown = await client.get(f"/v1/investigations/inv-{'a' * 32}", headers=AUTH_HEADERS)
    malformed = await client.get("/v1/investigations/../../etc", headers=AUTH_HEADERS)
    bad_id = await client.get("/v1/investigations/inv-XYZ", headers=AUTH_HEADERS)

    assert theirs.status_code == unknown.status_code == bad_id.status_code == 404
    assert (
        theirs.json()["error"]["code"]
        == unknown.json()["error"]["code"]
        == "investigation_not_found"
    )
    assert theirs.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert malformed.status_code == 404


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
async def test_rejects_invalid_requests_without_storing_them(
    client: httpx.AsyncClient, owner: Connection, body: bytes, status: int, code: str
) -> None:
    response = await client.post(
        "/v1/investigations",
        content=body,
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    cursor = await owner.execute("SELECT count(*) AS n FROM investigations")
    assert await cursor.fetchone() == {"n": 0}


async def test_rejects_other_media_types(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/investigations", content=b"incident=x", headers=AUTH_HEADERS)

    assert response.status_code == 415


async def test_rejects_oversized_bodies_with_and_without_a_length(pool: Pool) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b" " * 1024

    headers = {**AUTH_HEADERS, "Content-Type": "application/json"}
    async with serve(build_app(make_settings(max_request_bytes=4096), pool)) as client:
        declared = await client.post("/v1/investigations", content=b" " * 5000, headers=headers)
        chunked = await client.post("/v1/investigations", content=chunks(), headers=headers)

    assert declared.status_code == chunked.status_code == 413


async def test_an_idempotent_retry_returns_the_same_investigation(
    client: httpx.AsyncClient, owner: Connection
) -> None:
    first = await post(client, key="retry-key-1")
    second = await post(client, key="retry-key-1")

    assert first.status_code == second.status_code == 202
    assert first.json()["investigation_id"] == second.json()["investigation_id"]
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    cursor = await owner.execute("SELECT count(*) AS n FROM investigations")
    assert await cursor.fetchone() == {"n": 1}


async def test_reusing_a_key_for_a_different_request_conflicts(client: httpx.AsyncClient) -> None:
    await post(client, key="retry-key-2")

    response = await post(client, {"incident": "Something else entirely."}, key="retry-key-2")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_reused"


async def test_idempotency_keys_are_scoped_to_the_caller(client: httpx.AsyncClient) -> None:
    mine = await post(client, key="shared-key")
    theirs = await post(client, headers=OTHER_AUTH_HEADERS, key="shared-key")

    assert mine.json()["investigation_id"] != theirs.json()["investigation_id"]


async def test_concurrent_submissions_with_one_key_create_one_investigation(
    client: httpx.AsyncClient, owner: Connection
) -> None:
    responses = await asyncio.gather(*(post(client, key="burst-key") for _ in range(12)))

    assert {r.status_code for r in responses} == {202}
    assert len({r.json()["investigation_id"] for r in responses}) == 1
    cursor = await owner.execute("SELECT count(*) AS n FROM investigations")
    assert await cursor.fetchone() == {"n": 1}


@pytest.mark.parametrize("key", ["", "has space", "x" * 129, "semi;colon"])
async def test_rejects_malformed_idempotency_keys(client: httpx.AsyncClient, key: str) -> None:
    response = await client.post(
        "/v1/investigations", json=INCIDENT, headers={**AUTH_HEADERS, "Idempotency-Key": key}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


async def test_limits_unfinished_investigations_per_caller(pool: Pool) -> None:
    async with serve(
        build_app(make_settings(max_active_investigations_per_principal=2), pool)
    ) as client:
        accepted = [await post(client) for _ in range(2)]
        refused = await post(client)
        other = await post(client, headers=OTHER_AUTH_HEADERS)
        replay = await post(client, key="k")  # a new key still counts

    assert [r.status_code for r in accepted] == [202, 202]
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "active_investigation_limit"
    assert other.status_code == 202
    assert replay.status_code == 429


async def test_a_client_disconnect_after_acceptance_does_not_cancel(
    pool: Pool, owner: Connection
) -> None:
    app = build_app(make_settings(), pool)
    body = json.dumps(INCIDENT).encode()
    pending: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> Message:
        # The client is gone as soon as the body has been read.
        return pending.pop() if pending else {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/investigations",
        "raw_path": b"/v1/investigations",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"agent.test"),
            (b"authorization", f"Bearer {AGENT_API_KEY}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("agent.test", 80),
    }
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)

    assert sent[0]["status"] == 202
    investigation_id = json.loads(sent[1]["body"])["investigation_id"]
    assert (await investigation(owner, investigation_id))["status"] == "accepted"
    await run_worker(pool)
    assert (await investigation(owner, investigation_id))["status"] == "completed"


async def test_answers_503_when_the_database_is_unavailable(
    client: httpx.AsyncClient, pool: Pool
) -> None:
    await pool.close()

    created = await post(client)
    read = await client.get(f"/v1/investigations/inv-{'0' * 32}", headers=AUTH_HEADERS)

    assert created.status_code == read.status_code == 503
    assert created.json()["error"]["code"] == "persistence_unavailable"
