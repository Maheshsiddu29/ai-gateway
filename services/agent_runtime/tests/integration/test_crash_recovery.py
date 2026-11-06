"""Crash recovery with real process kills.

Each test runs ``tests/crash_worker.py`` as a subprocess that SIGKILLs itself at a
chosen point, so no cleanup, lease release, or outcome record happens, then runs a
clean worker on the same database and checks the result. The gateway is a real
HTTP server in this process that records every request it receives, so a duplicate
model call would be visible as a second request with the same X-Request-ID.
"""

import asyncio
import os
import signal
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import Connection, Pool, connect, create_pool
from agent_runtime.persistence.store import InvestigationStore
from tests.db import checkpoint_rows, insert_accepted, investigation, operation_states
from tests.servers import RecordingGateway, run_server

SIGKILL_EXIT = -signal.SIGKILL
# `python -m tests.crash_worker` runs from the service directory.
PACKAGE_ROOT = str(Path(__file__).parents[2])


@dataclass
class Harness:
    url: SecretStr
    schema: str
    gateway: RecordingGateway
    gateway_url: str
    pool: Pool
    owner: Connection
    started: list[asyncio.subprocess.Process] = field(default_factory=list)

    async def run_worker(
        self,
        *,
        point: str = "none",
        operation: str = "plan",
        stage: str = "",
        expect_kill: bool,
        env: dict[str, str] | None = None,
        wait_seconds: float = 60,
    ) -> int:
        """Run one worker subprocess to completion; returns its exit code."""
        process = await self.start_worker(point=point, operation=operation, stage=stage, env=env)
        returncode = await asyncio.wait_for(process.wait(), wait_seconds)
        if expect_kill:
            assert returncode == SIGKILL_EXIT, f"expected SIGKILL, got {returncode}"
        else:
            assert returncode == 0, f"worker failed with {returncode}"
        return returncode

    async def start_worker(
        self,
        *,
        point: str = "none",
        operation: str = "plan",
        stage: str = "",
        env: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.crash_worker",
            "--point",
            point,
            "--operation",
            operation,
            "--stage",
            stage,
            env={
                **os.environ,
                "AGENT_TEST_DATABASE_URL": self.url.get_secret_value(),
                "CRASH_SCHEMA": self.schema,
                "CRASH_GATEWAY_URL": self.gateway_url,
                **(env or {}),
            },
            cwd=PACKAGE_ROOT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.started.append(process)
        return process

    async def stop_all(self) -> None:
        """Kill and reap anything still running, so a failing test leaks nothing."""
        for process in self.started:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def row(self, investigation_id: str) -> dict[str, Any]:
        return await investigation(self.owner, investigation_id)

    async def states(self, investigation_id: str) -> list[tuple[str, str]]:
        return await operation_states(self.owner, investigation_id)


@pytest.fixture
async def harness(database_url: SecretStr) -> AsyncIterator[Harness]:
    schema = f"agent_crash_{uuid.uuid4().hex[:12]}"
    async with await connect(database_url, schema, "crash-tests") as owner:
        await migrations.upgrade(owner, schema)
        pool = create_pool(database_url, schema, "crash-tests", max_size=4, timeout=10)
        await pool.open(wait=True)
        gateway = RecordingGateway()
        harness = None
        try:
            async with run_server(gateway.app) as gateway_url:
                harness = Harness(database_url, schema, gateway, gateway_url, pool, owner)
                try:
                    yield harness
                finally:
                    # Before the gateway server stops, so no request is left in flight.
                    for event in gateway.hold.values():
                        event.set()
                    await harness.stop_all()
        finally:
            await pool.close()
            async with await connect(database_url, "public", "crash-tests") as admin:
                await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def request_counts(ids: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for request_id in ids:
        counts[request_id] = counts.get(request_id, 0) + 1
    return counts


async def test_a_crash_before_claiming_leaves_the_investigation_untouched(
    harness: Harness,
) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="before_claim", expect_kill=True)

    row = await harness.row(investigation_id)
    assert row["status"] == "accepted"
    assert row["attempts"] == 0
    assert row["lease_owner"] is None
    assert harness.gateway.models == []

    await harness.run_worker(expect_kill=False)
    assert (await harness.row(investigation_id))["status"] == "completed"
    assert harness.gateway.models == ["planner", "diagnosis"]


async def test_a_crash_after_claiming_leaves_a_lease_that_expires(harness: Harness) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(
        point="after_claim", expect_kill=True, env={"CRASH_LEASE_SECONDS": "60"}
    )

    row = await harness.row(investigation_id)
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert row["lease_owner"] is not None
    # A live lease blocks another worker until it expires.
    await harness.run_worker(expect_kill=False)
    assert (await harness.row(investigation_id))["status"] == "running"

    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() - interval '1 second'"
        " WHERE investigation_id = %s",
        (investigation_id,),
    )
    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert harness.gateway.models == ["planner", "diagnosis"]
    assert request_counts(harness.gateway.request_ids) == dict.fromkeys(
        harness.gateway.request_ids, 1
    )


@pytest.mark.parametrize("operation", ["plan", "diagnose"])
async def test_a_crash_before_an_intent_sends_the_call_once_on_resume(
    harness: Harness, operation: str
) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="before_intent", operation=operation, expect_kill=True)

    # Nothing is sent for the crashed operation; an earlier one may already have been.
    expected_before = [] if operation == "plan" else ["planner"]
    assert harness.gateway.models == expected_before
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    # The resumed worker sent only what was missing; the planner's reply, when it
    # was already recorded, came from the journal.
    assert harness.gateway.models == ["planner", "diagnosis"]
    assert request_counts(harness.gateway.request_ids) == dict.fromkeys(
        harness.gateway.request_ids, 1
    )
    assert await harness.states(investigation_id) == [
        ("plan", "succeeded"),
        ("diagnose", "succeeded"),
    ]


async def test_a_crash_after_an_intent_sends_that_call_once(harness: Harness) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="after_intent", expect_kill=True)

    assert harness.gateway.models == []  # dispatch is committed before the request
    assert await harness.states(investigation_id) == [("plan", "intent")]
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    await harness.run_worker(expect_kill=False)

    assert (await harness.row(investigation_id))["status"] == "completed"
    assert harness.gateway.models == ["planner", "diagnosis"]
    assert await harness.states(investigation_id) == [
        ("plan", "succeeded"),
        ("diagnose", "succeeded"),
    ]


@pytest.mark.parametrize(
    ("point", "operation", "sent"),
    [
        # Killed between committing dispatch and sending: nothing reached the
        # gateway, but the journal cannot know that, so it stays conservative.
        ("after_dispatch", "plan", []),
        ("after_dispatch", "diagnose", ["planner"]),
        # Killed with the reply in hand but not recorded.
        ("after_response", "plan", ["planner"]),
        ("after_response", "diagnose", ["planner", "diagnosis"]),
    ],
)
async def test_a_crash_after_dispatch_never_resends_the_call(
    harness: Harness, point: str, operation: str, sent: list[str]
) -> None:
    """A dispatched call may already have been generated and charged, so the
    investigation ends as reconciliation_required instead of calling again."""
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point=point, operation=operation, expect_kill=True)

    sent_before = list(harness.gateway.models)
    assert sent_before == sent
    assert (operation, "dispatched") in await harness.states(investigation_id)
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "reconciliation_required"
    assert row["failure_code"] == "llm_outcome_unknown"
    assert row["result"]["status"] == "reconciliation_required"
    assert row["lease_owner"] is None
    # No request was repeated, and nothing new was sent.
    assert harness.gateway.models == sent_before
    assert request_counts(harness.gateway.request_ids) == dict.fromkeys(
        harness.gateway.request_ids, 1
    )
    assert (operation, "uncertain") in await harness.states(investigation_id)
    # The unresolved liability stays reserved.
    assert row["reserved_micro_usd"] > 0


async def test_a_crash_after_recording_the_outcome_reuses_the_recorded_reply(
    harness: Harness,
) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="after_outcome", expect_kill=True)

    assert harness.gateway.models == ["planner"]
    assert await harness.states(investigation_id) == [("plan", "succeeded")]
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    # The planner was not called again: its reply came from the journal.
    assert harness.gateway.models == ["planner", "diagnosis"]
    assert row["result"]["diagnosis"]["conclusion"] == "supported"


@pytest.mark.parametrize(
    "stage", ["validate_plan", "collect_diagnostics", "diagnose", "validate_diagnosis", "finalize"]
)
async def test_a_crash_between_stages_resumes_at_that_stage(harness: Harness, stage: str) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="before_stage", stage=stage, expect_kill=True)

    sent_before = list(harness.gateway.models)
    assert (await harness.row(investigation_id))["status"] == "running"
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed", row["result"]
    assert row["result"]["diagnosis"]["conclusion"] == "supported"
    # Stages already finished are not repeated: each model call was sent once.
    assert harness.gateway.models[: len(sent_before)] == sent_before
    assert len(harness.gateway.models) == 2
    assert request_counts(harness.gateway.request_ids) == dict.fromkeys(
        harness.gateway.request_ids, 1
    )
    assert await checkpoint_rows(harness.owner, investigation_id) == 0


async def test_a_crash_before_the_terminal_write_finishes_on_resume(harness: Harness) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="before_finish", expect_kill=True)

    assert harness.gateway.models == ["planner", "diagnosis"]
    assert (await harness.row(investigation_id))["status"] == "running"
    assert await checkpoint_rows(harness.owner, investigation_id) > 0
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    assert row["result"]["diagnosis"]["conclusion"] == "supported"
    # The model calls were replayed from the journal, not sent again.
    assert harness.gateway.models == ["planner", "diagnosis"]


async def test_a_crash_after_the_terminal_write_keeps_the_result(harness: Harness) -> None:
    investigation_id = await insert_accepted(harness.owner)

    await harness.run_worker(point="after_final_checkpoint", expect_kill=True)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    assert row["result"]["diagnosis"]["conclusion"] == "supported"

    # A later worker leaves the terminal investigation alone, even with leftover
    # checkpoints, because terminal rows are never claimed.
    await harness.run_worker(expect_kill=False)
    assert (await harness.row(investigation_id))["status"] == "completed"
    assert harness.gateway.models == ["planner", "diagnosis"]


async def test_two_workers_racing_for_one_investigation_run_it_once(harness: Harness) -> None:
    investigation_id = await insert_accepted(harness.owner)

    processes = [await harness.start_worker() for _ in range(4)]
    codes = await asyncio.gather(*(asyncio.wait_for(process.wait(), 60) for process in processes))

    assert codes == [0, 0, 0, 0]
    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 1
    assert harness.gateway.models == ["planner", "diagnosis"]
    assert request_counts(harness.gateway.request_ids) == dict.fromkeys(
        harness.gateway.request_ids, 1
    )


async def test_many_investigations_are_each_run_once_by_competing_workers(
    harness: Harness,
) -> None:
    ids = [await insert_accepted(harness.owner) for _ in range(6)]

    processes = [await harness.start_worker(env={"CRASH_CONCURRENCY": "2"}) for _ in range(3)]
    await asyncio.gather(*(asyncio.wait_for(process.wait(), 120) for process in processes))

    for investigation_id in ids:
        row = await harness.row(investigation_id)
        assert row["status"] == "completed", (investigation_id, row["result"])
        assert row["attempts"] == 1
    assert sorted(harness.gateway.models) == ["diagnosis"] * 6 + ["planner"] * 6
    assert request_counts(harness.gateway.request_ids) == dict.fromkeys(
        harness.gateway.request_ids, 1
    )


async def test_a_stale_worker_cannot_finish_after_another_took_over(harness: Harness) -> None:
    """A worker whose lease is taken while its model call is in flight must write
    nothing; the investigation's outcome belongs to the new owner."""
    investigation_id = await insert_accepted(harness.owner)
    harness.gateway.hold["planner"] = asyncio.Event()

    # A worker with a short lease stalls inside its planner call. Its heartbeat
    # keeps renewing, so the lease is taken from it here instead.
    stalled = await harness.start_worker(
        env={"CRASH_LEASE_SECONDS": "3", "CRASH_GRACE_SECONDS": "0"}
    )
    await asyncio.wait_for(harness.gateway.arrived.wait(), 30)
    await harness.owner.execute(
        "UPDATE investigations SET lease_expires_at = now() - interval '1 second'"
        " WHERE investigation_id = %s",
        (investigation_id,),
    )
    await harness.run_worker(expect_kill=False)
    row = await harness.row(investigation_id)
    assert row["status"] == "reconciliation_required"
    assert row["failure_code"] == "llm_outcome_unknown"
    fencing_token = row["fencing_token"]

    # The stalled worker's call now returns; it must not change anything.
    harness.gateway.hold["planner"].set()
    assert await asyncio.wait_for(stalled.wait(), 60) == 0

    after = await harness.row(investigation_id)
    assert after["status"] == "reconciliation_required"
    assert after["fencing_token"] == fencing_token
    assert after["result"] == row["result"]
    assert harness.gateway.models == ["planner"]


async def test_a_database_outage_leaves_the_investigation_claimable(harness: Harness) -> None:
    investigation_id = await insert_accepted(harness.owner)
    store = InvestigationStore(harness.pool)
    # A worker claims the investigation and then loses the database, so it can
    # neither finish nor release it. Its lease expires on the server's clock.
    claim = await store.claim(
        "lost-worker",
        lease_seconds=3,
        max_running=10,
        workflow_version=(await harness.row(investigation_id))["workflow_version"],
    )
    assert claim is not None
    await asyncio.sleep(4)

    await harness.run_worker(expect_kill=False)

    row = await harness.row(investigation_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert harness.gateway.models == ["planner", "diagnosis"]


async def test_sigterm_during_a_model_call_does_not_lose_the_investigation(
    harness: Harness,
) -> None:
    investigation_id = await insert_accepted(harness.owner)
    harness.gateway.hold["planner"] = asyncio.Event()

    worker = await harness.start_worker(env={"CRASH_RUN_FOREVER": "1", "CRASH_GRACE_SECONDS": "10"})
    await asyncio.wait_for(harness.gateway.arrived.wait(), 30)
    worker.send_signal(signal.SIGTERM)
    await asyncio.sleep(0.3)
    # The call finishes inside the grace period, so the worker releases cleanly.
    harness.gateway.hold["planner"].set()
    assert await asyncio.wait_for(worker.wait(), 60) == 0

    row = await harness.row(investigation_id)
    assert row["status"] == "running"
    assert row["lease_owner"] is None
    assert await harness.states(investigation_id) == [("plan", "succeeded")]

    await harness.run_worker(expect_kill=False)

    assert (await harness.row(investigation_id))["status"] == "completed"
    # The planner's reply came from the journal.
    assert harness.gateway.models == ["planner", "diagnosis"]
