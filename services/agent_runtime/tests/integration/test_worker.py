"""The durable worker against PostgreSQL, with a scripted gateway that counts requests.

Process deaths (SIGKILL) are covered by tests/integration/test_crash_recovery.py;
these tests cover leases, fencing, deadlines, shutdown, and recovery in-process.
"""

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agent_runtime.diagnostics.tools import DiagnosticExecutor
from agent_runtime.durable.worker import Worker, WorkerConfig
from agent_runtime.gateway_client import GatewayClient, GatewayTimeouts
from agent_runtime.investigation.graph import WORKFLOW_VERSION, ModelAliases, OperationType
from agent_runtime.investigation.schemas import InvestigationResult
from agent_runtime.persistence.checkpoints import checkpointer
from agent_runtime.persistence.database import Connection, Pool
from agent_runtime.persistence.store import InvestigationStore, Lease, LeaseLostError, Operation
from tests.db import checkpoint_rows, insert_accepted, investigation, operation_states
from tests.fakes import (
    DIAGNOSIS_REPLY,
    GATEWAY_API_KEY,
    PLAN_REPLY,
    CountingBackend,
    GatewayStub,
    completion_body,
    example_result,
)

USAGE = {"prompt_tokens": 30, "completion_tokens": 6}


class ScriptedGateway:
    """Answers planner and diagnosis calls; a model can be made to wait for a release."""

    def __init__(self) -> None:
        self.requests: Counter[str] = Counter()
        self.arrived: dict[str, asyncio.Event] = {
            "planner": asyncio.Event(),
            "diagnosis": asyncio.Event(),
        }
        self.hold: dict[str, asyncio.Event] = {}
        # Set to None to answer without token usage.
        self.usage: dict[str, int] | None = USAGE
        self.stub = GatewayStub(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        self.requests[model] += 1
        self.arrived[model].set()
        if model in self.hold:
            await self.hold[model].wait()
        reply = PLAN_REPLY if model == "planner" else DIAGNOSIS_REPLY
        return httpx.Response(200, json=completion_body(reply, model=model, usage=self.usage))

    def client(self) -> GatewayClient:
        return GatewayClient(
            "http://gateway.test",
            SecretStr(GATEWAY_API_KEY),
            timeouts=GatewayTimeouts(),
            max_connections=8,
            max_request_bytes=262_144,
            max_response_bytes=65_536,
            transport=self.stub,
        )


@asynccontextmanager
async def worker(
    pool: Pool,
    gateway: ScriptedGateway,
    backend: CountingBackend | None = None,
    store: InvestigationStore | None = None,
    **overrides: Any,
) -> AsyncIterator[Worker]:
    values: dict[str, Any] = {
        "worker_id": "worker-a",
        "concurrency": 2,
        "max_running": 100,
        "lease_seconds": 3.0,
        "poll_seconds": 0.05,
        "shutdown_grace_seconds": 5.0,
        "max_recovery_attempts": 3,
        "models": ModelAliases("planner", "diagnosis"),
        "allowed_paths": frozenset({"/"}),
        **overrides,
    }
    client = gateway.client()
    try:
        yield Worker(
            store or InvestigationStore(pool),
            checkpointer(pool),
            client,
            DiagnosticExecutor(
                backend or CountingBackend(), timeout_seconds=5, max_output_bytes=16_384
            ),
            WorkerConfig(**values),
        )
    finally:
        await client.aclose()


class DiagnoseDispatchFailsOnce(InvestigationStore):
    """Fails like a crash right after the diagnose intent was recorded, once."""

    failed = False

    async def mark_dispatched(self, lease: Lease, operation: Operation) -> None:
        if operation.operation_type is OperationType.DIAGNOSE and not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after the intent was recorded")
        await super().mark_dispatched(lease, operation)


async def claim(
    store: InvestigationStore, worker_id: str = "w", max_running: int = 100
) -> Lease | None:
    claimed = await store.claim(
        worker_id, lease_seconds=30, max_running=max_running, workflow_version=WORKFLOW_VERSION
    )
    return None if claimed is None else claimed.lease


async def test_runs_an_accepted_investigation_to_completion(owner: Connection, pool: Pool) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()
    backend = CountingBackend()

    async with worker(pool, gateway, backend) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert row["status"] == "completed"
    assert row["result"]["diagnosis"]["conclusion"] == "supported"
    assert row["result"]["usage"]["llm_calls"] == 2
    assert row["result"]["usage"]["prompt_tokens"] == 60
    assert row["incident"] is None
    assert row["lease_owner"] is None
    assert gateway.requests == {"planner": 1, "diagnosis": 1}
    assert len(backend.requests) == 2
    assert await operation_states(owner, investigation_id) == [
        ("plan", "succeeded"),
        ("diagnose", "succeeded"),
    ]
    cursor = await owner.execute(
        "SELECT count(*) AS n FROM operations WHERE investigation_id = %s AND reply IS NOT NULL",
        (investigation_id,),
    )
    assert (await cursor.fetchone()) == {"n": 0}
    assert await checkpoint_rows(owner, investigation_id) == 0


async def test_concurrent_claims_are_exclusive(owner: Connection, pool: Pool) -> None:
    ids = {await insert_accepted(owner) for _ in range(30)}
    stores = [InvestigationStore(pool) for _ in range(6)]

    async def drain(store: InvestigationStore, name: str) -> list[Lease]:
        leases = []
        while (lease := await claim(store, name)) is not None:
            leases.append(lease)
        return leases

    results = await asyncio.gather(*(drain(s, f"w{i}") for i, s in enumerate(stores)))

    claimed = [lease.investigation_id for leases in results for lease in leases]
    assert sorted(claimed) == sorted(ids)
    assert {lease.fencing_token for leases in results for lease in leases} == {1}


async def test_the_running_limit_holds_across_concurrent_workers(
    owner: Connection, pool: Pool
) -> None:
    for _ in range(10):
        await insert_accepted(owner)
    stores = [InvestigationStore(pool) for _ in range(10)]

    leases = await asyncio.gather(*(claim(s, f"w{i}", max_running=3) for i, s in enumerate(stores)))

    assert sum(lease is not None for lease in leases) == 3


async def test_an_expired_lease_is_reclaimed_and_fences_out_the_old_owner(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    store = InvestigationStore(pool)
    first = await claim(store, "worker-a")
    assert first is not None
    assert await claim(store, "worker-b") is None

    await owner.execute(
        "UPDATE investigations SET lease_expires_at = now() - interval '1 second'"
        " WHERE investigation_id = %s",
        (investigation_id,),
    )
    second = await claim(store, "worker-b")

    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    assert not await store.renew(first, 30)
    with pytest.raises(LeaseLostError):
        await store.enter_stage(first, "plan")
    with pytest.raises(LeaseLostError):
        await store.release(first)
    assert await store.renew(second, 30)
    assert (await investigation(owner, investigation_id))["lease_owner"] == "worker-b"


async def test_resumes_from_its_checkpoint_after_a_failure_before_dispatch(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway, backend = ScriptedGateway(), CountingBackend()
    store = DiagnoseDispatchFailsOnce(pool)
    async with worker(pool, gateway, backend, store=store) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert gateway.requests == {"planner": 1, "diagnosis": 1}
    assert len(backend.requests) == 2  # diagnostics were not collected again
    assert await operation_states(owner, investigation_id) == [
        ("plan", "succeeded"),
        ("diagnose", "succeeded"),
    ]


class FinishFailsOnce(InvestigationStore):
    """Fails like a crash inside finalize, before the terminal write, once."""

    failed = False

    async def finish(self, lease: Lease, result: InvestigationResult) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash in finalize")
        await super().finish(lease, result)


async def test_a_checkpoint_without_a_recorded_result_is_finished_on_resume(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()

    async with worker(pool, gateway, store=FinishFailsOnce(pool)) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert gateway.requests == {"planner": 1, "diagnosis": 1}


async def test_a_lost_lease_during_a_call_leads_to_reconciliation_not_a_resend(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()
    gateway.hold["diagnosis"] = asyncio.Event()  # never released: the call hangs

    async with worker(pool, gateway, worker_id="worker-a") as first:
        running = asyncio.create_task(first.run_until_idle())
        await gateway.arrived["diagnosis"].wait()
        # worker-a's lease lapses (it stalled) and worker-b takes over.
        await owner.execute(
            "UPDATE investigations SET lease_expires_at = now() - interval '1 second'"
            " WHERE investigation_id = %s",
            (investigation_id,),
        )
        async with worker(pool, gateway, worker_id="worker-b") as second:
            await second.run_until_idle()
        # worker-a notices at its next heartbeat and stops without writing.
        await asyncio.wait_for(running, timeout=10)

    row = await investigation(owner, investigation_id)
    assert row["status"] == "reconciliation_required"
    assert row["failure_code"] == "llm_outcome_unknown"
    assert row["lease_owner"] is None
    assert row["result"]["status"] == "reconciliation_required"
    assert gateway.requests == {"planner": 1, "diagnosis": 1}
    assert await operation_states(owner, investigation_id) == [
        ("plan", "succeeded"),
        ("diagnose", "uncertain"),
    ]


async def test_the_deadline_ends_an_investigation_during_a_model_call(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner, deadline_seconds=1)
    gateway = ScriptedGateway()
    gateway.hold["planner"] = asyncio.Event()

    async with worker(pool, gateway) as w:
        await asyncio.wait_for(w.run_until_idle(), timeout=10)

    row = await investigation(owner, investigation_id)
    assert (row["status"], row["failure_code"]) == ("failed", "investigation_timeout")
    assert row["result"]["failure"]["stage"] == "planning"
    assert gateway.requests == {"planner": 1}
    cursor = await owner.execute(
        "SELECT state, failure_code, may_have_executed FROM operations WHERE investigation_id = %s",
        (investigation_id,),
    )
    assert await cursor.fetchall() == [
        {"state": "failed", "failure_code": "investigation_timeout", "may_have_executed": True}
    ]


async def test_an_investigation_past_its_deadline_is_not_started(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner, deadline_seconds=-1)
    gateway = ScriptedGateway()

    async with worker(pool, gateway) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert (row["status"], row["failure_code"]) == ("failed", "investigation_timeout")
    assert gateway.requests == {}


async def test_recovery_attempts_are_limited(owner: Connection, pool: Pool) -> None:
    investigation_id = await insert_accepted(owner)
    for _ in range(3):
        lease = await claim(InvestigationStore(pool))
        assert lease is not None
        await InvestigationStore(pool).release(lease)
    gateway = ScriptedGateway()

    async with worker(pool, gateway, max_recovery_attempts=3) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert (row["status"], row["failure_code"]) == ("failed", "recovery_attempts_exhausted")
    assert row["attempts"] == 4
    assert gateway.requests == {}


async def test_shutdown_releases_at_the_next_node_and_another_worker_finishes(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()
    gateway.hold["planner"] = asyncio.Event()

    async with worker(pool, gateway) as first:
        running = asyncio.create_task(first.run())
        await gateway.arrived["planner"].wait()
        first.request_shutdown()
        await asyncio.sleep(0.1)
        gateway.hold["planner"].set()  # the call in flight finishes within the grace period
        await asyncio.wait_for(running, timeout=10)

    row = await investigation(owner, investigation_id)
    assert row["status"] == "running"
    assert row["lease_owner"] is None
    # Released before validate_plan was entered; the plan's checkpoint is kept.
    assert row["current_stage"] == "plan"

    async with worker(pool, gateway, worker_id="worker-b") as second:
        await second.run_until_idle()
    assert (await investigation(owner, investigation_id))["status"] == "completed"
    assert gateway.requests == {"planner": 1, "diagnosis": 1}


async def test_a_call_still_running_when_the_grace_period_ends_is_not_resent(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()
    gateway.hold["planner"] = asyncio.Event()  # never released

    async with worker(pool, gateway, shutdown_grace_seconds=0.2) as first:
        running = asyncio.create_task(first.run())
        await gateway.arrived["planner"].wait()
        first.request_shutdown()
        await asyncio.wait_for(running, timeout=10)

    assert (await investigation(owner, investigation_id))["lease_owner"] is None
    async with worker(pool, gateway, worker_id="worker-b") as second:
        await second.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert (row["status"], row["failure_code"]) == ("failed", "worker_shutdown_interrupted")
    assert gateway.requests == {"planner": 1}


async def test_an_unreadable_checkpoint_fails_the_investigation(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()
    store = DiagnoseDispatchFailsOnce(pool)
    store.failed = True  # not this time
    gateway.hold["diagnosis"] = asyncio.Event()
    async with worker(pool, gateway, shutdown_grace_seconds=0.1) as first:
        running = asyncio.create_task(first.run())
        await gateway.arrived["diagnosis"].wait()
        first.request_shutdown()
        await asyncio.wait_for(running, timeout=10)
    await owner.execute(
        "UPDATE checkpoint_blobs SET blob = '\\xc1c1c1' WHERE thread_id = %s AND type = 'msgpack'",
        (investigation_id,),
    )

    async with worker(pool, gateway, worker_id="worker-b") as second:
        await second.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert (row["status"], row["failure_code"]) == ("failed", "checkpoint_invalid")
    assert gateway.requests == {"planner": 1, "diagnosis": 1}


async def test_a_stale_worker_cannot_finish_an_investigation(owner: Connection, pool: Pool) -> None:
    investigation_id = await insert_accepted(owner)
    store = InvestigationStore(pool)
    stale = await claim(store, "worker-a")
    assert stale is not None
    await owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )
    assert await claim(store, "worker-b") is not None
    gateway = ScriptedGateway()

    with pytest.raises(LeaseLostError):
        await store.finish(stale, example_result(investigation_id))
    assert (await investigation(owner, investigation_id))["status"] == "running"
    assert gateway.requests == {}
