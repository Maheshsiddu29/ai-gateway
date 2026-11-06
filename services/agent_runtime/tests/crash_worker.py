"""A worker process that kills itself with SIGKILL at a chosen point.

Run by tests/integration/test_crash_recovery.py as

    python -m tests.crash_worker --point after_dispatch --operation plan

with AGENT_TEST_DATABASE_URL, CRASH_SCHEMA, and CRASH_GATEWAY_URL in the
environment. It processes the queue like the real worker, except that the
investigation store and checkpointer are wrapped so that reaching the point
ends the process immediately: no cleanup, no lease release, no outcome
recorded, exactly as a machine or OOM kill would.
"""

import argparse
import asyncio
import os
import signal
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr

from agent_runtime.diagnostics.tools import DiagnosticExecutor
from agent_runtime.durable.worker import Worker, WorkerConfig
from agent_runtime.gateway_client import GatewayClient, GatewayTimeouts
from agent_runtime.investigation.graph import ModelAliases, OperationType
from agent_runtime.investigation.schemas import InvestigationResult
from agent_runtime.persistence.checkpoints import strict_serializer
from agent_runtime.persistence.database import create_pool
from agent_runtime.persistence.store import (
    Claim,
    InvestigationStore,
    Lease,
    ObservedUsage,
    Operation,
)
from tests.fakes import CountingBackend

POINTS = (
    "before_claim",
    "after_claim",
    "before_intent",
    "after_intent",
    "after_dispatch",
    "after_response",
    "after_outcome",
    "before_stage",
    "before_finish",
    "after_final_checkpoint",
    "none",
)


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


class CrashingStore(InvestigationStore):
    def __init__(
        self, pool: Any, point: str, operation: str, stage: str, saver: "CrashingSaver"
    ) -> None:
        super().__init__(pool)
        self.saver = saver
        self.point = point
        self.operation = OperationType(operation)
        self.stage = stage

    async def claim(self, worker_id: str, **kwargs: Any) -> Claim | None:
        if self.point == "before_claim":
            die()
        claim = await super().claim(worker_id, **kwargs)
        if claim is not None and self.point == "after_claim":
            die()
        return claim

    async def create_intent(
        self, lease: Lease, operation_type: OperationType, *args: Any
    ) -> Operation:
        mine = operation_type is self.operation
        if mine and self.point == "before_intent":
            die()
        operation = await super().create_intent(lease, operation_type, *args)
        if mine and self.point == "after_intent":
            die()
        return operation

    async def mark_dispatched(self, lease: Lease, operation: Operation) -> None:
        await super().mark_dispatched(lease, operation)
        if operation.operation_type is self.operation and self.point == "after_dispatch":
            die()

    async def record_success(
        self, lease: Lease, operation: Operation, reply: str, usage: ObservedUsage | None
    ) -> None:
        mine = operation.operation_type is self.operation
        if mine and self.point == "after_response":
            die()  # the reply arrived but is not recorded
        await super().record_success(lease, operation, reply, usage)
        if mine and self.point == "after_outcome":
            die()

    async def enter_stage(self, lease: Lease, stage: str) -> float:
        if self.point == "before_stage" and stage == self.stage:
            die()  # the previous node's checkpoint is already written
        return await super().enter_stage(lease, stage)

    async def finish(self, lease: Lease, result: InvestigationResult) -> None:
        if self.point == "before_finish":
            die()
        await super().finish(lease, result)
        self.saver.finished = True


class CrashingSaver(AsyncPostgresSaver):
    """Dies while writing the checkpoint that follows the terminal transition."""

    point = "none"
    finished = False

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        if self.point == "after_final_checkpoint" and self.finished:
            die()
        return await super().aput(*args, **kwargs)


async def main(point: str, operation: str, stage: str) -> None:
    pool = create_pool(
        SecretStr(os.environ["AGENT_TEST_DATABASE_URL"]),
        os.environ["CRASH_SCHEMA"],
        "crash-worker",
        max_size=4,
        timeout=10,
    )
    await pool.open(wait=True)
    saver = CrashingSaver(pool, serde=strict_serializer())
    saver.point = point
    store = CrashingStore(pool, point, operation, stage, saver)
    client = GatewayClient(
        os.environ["CRASH_GATEWAY_URL"],
        SecretStr("crash-test-gateway-key"),
        timeouts=GatewayTimeouts(read=30),
        max_connections=4,
        max_request_bytes=262_144,
        max_response_bytes=65_536,
    )
    worker = Worker(
        store,
        saver,
        client,
        DiagnosticExecutor(CountingBackend(), timeout_seconds=5, max_output_bytes=16_384),
        WorkerConfig(
            worker_id=f"crash-worker-{os.getpid()}",
            concurrency=int(os.environ.get("CRASH_CONCURRENCY", "1")),
            max_running=100,
            lease_seconds=float(os.environ.get("CRASH_LEASE_SECONDS", "30")),
            poll_seconds=0.05,
            shutdown_grace_seconds=float(os.environ.get("CRASH_GRACE_SECONDS", "5")),
            max_recovery_attempts=3,
            models=ModelAliases("planner", "diagnosis"),
            allowed_paths=frozenset({"/"}),
        ),
    )
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, worker.request_shutdown)
    try:
        if os.environ.get("CRASH_RUN_FOREVER") == "1":
            await worker.run()
        else:
            await worker.run_until_idle()
    finally:
        await client.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--point", choices=POINTS, default="none")
    parser.add_argument("--operation", choices=["plan", "diagnose"], default="plan")
    parser.add_argument("--stage", default="")
    args = parser.parse_args()
    asyncio.run(main(args.point, args.operation, args.stage))
