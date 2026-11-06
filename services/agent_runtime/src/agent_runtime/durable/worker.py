"""The durable worker: claims investigations from PostgreSQL and runs the graph.

A worker runs up to ``concurrency`` investigations at once. For each claimed
investigation it holds a lease, renews it at a third of its length, and runs the
graph with checkpoints written after every node. It stops working on an
investigation, without writing anything, as soon as its lease may have lapsed.

Why an execution stops, and what happens to the investigation:

* it finishes: ``finalize`` made it terminal; its checkpoints are deleted.
* the deadline passes: it fails with ``investigation_timeout`` (a model call in
  flight is recorded as possibly executed).
* shutdown: at the next node boundary the lease is released for another worker; a
  model call still running when the grace period ends is cancelled and recorded,
  and the next owner replays that failure.
* the lease is lost: nothing is written; the next owner decides from the journal.
* its checkpoint is unreadable or invalid: it fails with ``checkpoint_invalid``.
* anything else: the error is logged and the lease released; every claim counts
  against ``max_recovery_attempts``.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import ormsgpack
import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import PoolTimeout

from agent_runtime.diagnostics.tools import DiagnosticExecutor
from agent_runtime.durable.journal import JournaledLlmCaller
from agent_runtime.investigation.graph import (
    STAGE_OF,
    WORKFLOW_VERSION,
    CheckpointStateError,
    CompiledGraph,
    InvestigationState,
    ModelAliases,
    Node,
    StageContext,
    build_graph,
    build_result,
)
from agent_runtime.investigation.prompts import PROMPT_VERSION
from agent_runtime.investigation.schemas import Failure, InvestigationResult, Usage
from agent_runtime.investigation.workflow import CompletionClient
from agent_runtime.persistence.store import Claim, InvestigationStore, LeaseLostError

logger = logging.getLogger(__name__)

_DATABASE_ERRORS = (psycopg.Error, PoolTimeout, OSError)
# A checkpoint that cannot be decoded, or decodes to state this graph never writes.
_CHECKPOINT_ERRORS = (CheckpointStateError, ormsgpack.MsgpackDecodeError)


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    concurrency: int
    max_running: int
    lease_seconds: float
    poll_seconds: float
    shutdown_grace_seconds: float
    max_recovery_attempts: int
    models: ModelAliases
    allowed_paths: frozenset[str]


class _Cause(StrEnum):
    DEADLINE = "investigation_timeout"
    SHUTDOWN = "worker_shutdown_interrupted"
    LEASE_LOST = "lease_lost"


class _ReleasedError(Exception):
    """Raised at a node boundary during shutdown."""


class _DeadlineError(Exception):
    """Raised at a node boundary once the deadline has passed."""


class Worker:
    def __init__(
        self,
        store: InvestigationStore,
        checkpoints: AsyncPostgresSaver,
        gateway: CompletionClient,
        diagnostics: DiagnosticExecutor,
        config: WorkerConfig,
    ) -> None:
        self._store = store
        self._checkpoints = checkpoints
        self._gateway = gateway
        self._diagnostics = diagnostics
        self._config = config
        self._graph: CompiledGraph = build_graph().compile(checkpointer=checkpoints)
        self._stopping = asyncio.Event()
        self._executions: dict[asyncio.Task[None], _Execution] = {}

    def request_shutdown(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Claim and run investigations until shutdown is requested, then drain."""
        stop = asyncio.ensure_future(self._stopping.wait())
        try:
            while not self._stopping.is_set():
                await self._claim_available()
                await asyncio.wait(
                    {stop, *self._executions},
                    timeout=self._config.poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            stop.cancel()
            await self._drain()

    async def run_until_idle(self) -> None:
        """Claim and run until nothing is claimable and nothing runs. For tests and
        one-shot processing."""
        while True:
            await self._claim_available()
            if not self._executions:
                return
            await asyncio.wait(set(self._executions), return_when=asyncio.FIRST_COMPLETED)

    async def _claim_available(self) -> None:
        while len(self._executions) < self._config.concurrency and not self._stopping.is_set():
            try:
                claim = await self._store.claim(
                    self._config.worker_id,
                    lease_seconds=self._config.lease_seconds,
                    max_running=self._config.max_running,
                    workflow_version=WORKFLOW_VERSION,
                )
            except _DATABASE_ERRORS as exc:
                logger.error("claiming failed", extra={"error": type(exc).__name__})
                return
            if claim is None:
                return
            execution = _Execution(self, claim)
            task = asyncio.create_task(execution.run())
            self._executions[task] = execution
            task.add_done_callback(self._executions.pop)

    async def _drain(self) -> None:
        if not self._executions:
            return
        for execution in self._executions.values():
            execution.release_at_next_node = True
        _, pending = await asyncio.wait(
            set(self._executions), timeout=self._config.shutdown_grace_seconds
        )
        for task in pending:
            self._executions[task].interrupt(_Cause.SHUTDOWN)
        await asyncio.gather(*self._executions, return_exceptions=True)


class _Execution:
    """One claimed investigation, from claim to finish, release, or lease loss."""

    def __init__(self, worker: Worker, claim: Claim) -> None:
        self._worker = worker
        self._claim = claim
        self._lease = claim.lease
        self._config: RunnableConfig = {"configurable": {"thread_id": claim.lease.investigation_id}}
        self._node: Node = "plan"
        self._cause: _Cause | None = None
        self._finished = False
        self._main: asyncio.Task[None] | None = None
        self.release_at_next_node = False

    def interrupt(self, cause: _Cause) -> None:
        if self._cause is None:
            self._cause = cause
        if self._main is not None:
            self._main.cancel()

    def _interruption(self) -> str | None:
        """The failure code a cancelled model call records, if it may record one."""
        if self._cause in (_Cause.DEADLINE, _Cause.SHUTDOWN):
            return self._cause.value
        return None

    async def run(self) -> None:
        investigation_id = self._lease.investigation_id
        logger.info(
            "investigation claimed",
            extra={
                "investigation_id": investigation_id,
                "worker_id": self._lease.worker_id,
                "fencing_token": self._lease.fencing_token,
                "attempt": self._claim.attempts,
            },
        )
        loop = asyncio.get_running_loop()
        deadline = loop.call_later(
            max(self._claim.remaining_seconds, 0), self.interrupt, _Cause.DEADLINE
        )
        heartbeat = asyncio.create_task(self._heartbeat())
        self._main = asyncio.create_task(self._investigate())
        try:
            await asyncio.wait({self._main})
        finally:
            deadline.cancel()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        try:
            await self._conclude(self._main)
        except LeaseLostError:
            logger.warning("lease lost", extra={"investigation_id": investigation_id})
        except _DATABASE_ERRORS as exc:
            logger.error(
                "could not record the investigation's outcome",
                extra={"investigation_id": investigation_id, "error": type(exc).__name__},
            )

    async def _investigate(self) -> None:
        if self._claim.attempts > self._worker._config.max_recovery_attempts:
            await self._fail(
                "recovery_attempts_exhausted",
                f"The investigation was claimed {self._claim.attempts} times without finishing.",
            )
            return
        if self._claim.remaining_seconds <= 0:
            raise _DeadlineError
        existing = await self._worker._checkpoints.aget_tuple(self._config)
        await self._worker._graph.ainvoke(
            None if existing is not None else {},
            self._config,
            context=self._context(),
            durability="sync",
        )
        if not self._finished:
            # The graph had already ended, but its result was never recorded.
            state = await self._state()
            if "failure" not in state and "diagnosis" not in state:
                raise CheckpointStateError("the graph ended without a diagnosis or a failure")
            await self._finish(await build_result(state, self._context()))

    async def _conclude(self, main: asyncio.Task[None]) -> None:
        if main.cancelled() or isinstance(main.exception(), _DeadlineError):
            cause = self._cause or _Cause.DEADLINE
            if cause is _Cause.DEADLINE:
                await self._fail("investigation_timeout", "The investigation passed its deadline.")
            elif cause is _Cause.SHUTDOWN:
                await self._release()
            return
        error = main.exception()
        if error is None:
            if self._finished:
                await self._delete_checkpoints()
            return
        if isinstance(error, _ReleasedError):
            await self._release()
        elif isinstance(error, LeaseLostError):
            raise error
        elif isinstance(error, _CHECKPOINT_ERRORS):
            logger.error(
                "invalid checkpoint",
                extra={
                    "investigation_id": self._lease.investigation_id,
                    "error": type(error).__name__,
                },
            )
            await self._fail("checkpoint_invalid", "The investigation's saved state is invalid.")
        elif isinstance(error, _DATABASE_ERRORS):
            # The lease expires and the investigation is claimed again.
            raise error
        else:
            logger.error(
                "investigation execution failed",
                extra={"investigation_id": self._lease.investigation_id},
                exc_info=error,
            )
            await self._release()

    async def _release(self) -> None:
        await self._worker._store.release(self._lease)
        logger.info(
            "investigation released", extra={"investigation_id": self._lease.investigation_id}
        )

    def _context(self) -> StageContext:
        worker = self._worker
        store = worker._store
        lease = self._lease

        async def incident() -> str:
            return await store.incident(lease)

        async def enter(node: Node) -> None:
            self._node = node
            if self.release_at_next_node and node != "finalize":
                raise _ReleasedError
            if await store.enter_stage(lease, node) <= 0:
                raise _DeadlineError

        async def elapsed_ms() -> float:
            return await store.elapsed_ms(lease.investigation_id)

        return StageContext(
            investigation_id=lease.investigation_id,
            llm=JournaledLlmCaller(
                store,
                lease,
                worker._gateway,
                max_output_tokens=self._claim.max_output_tokens,
                interruption=self._interruption,
            ),
            diagnostics=worker._diagnostics,
            models=worker._config.models,
            max_tool_calls=self._claim.max_tool_calls,
            allowed_paths=worker._config.allowed_paths,
            incident=incident,
            enter=enter,
            elapsed_ms=elapsed_ms,
            finish=self._finish,
        )

    async def _finish(self, result: InvestigationResult) -> None:
        await self._worker._store.finish(self._lease, result)
        self._finished = True
        logger.info(
            "investigation finished",
            extra={
                "investigation_id": self._lease.investigation_id,
                "status": result.status,
                "failure_code": result.failure.code if result.failure else None,
                "llm_calls": result.usage.llm_calls,
                "tool_calls": result.usage.tool_calls,
            },
        )

    async def _state(self) -> InvestigationState:
        snapshot = await self._worker._graph.aget_state(self._config)
        # A snapshot's values are the graph's state channels.
        return cast(InvestigationState, snapshot.values)

    async def _fail(self, code: str, message: str) -> None:
        """Finish as failed, keeping whatever valid plan and evidence were saved."""
        failure = Failure(stage=STAGE_OF[self._node], code=code, message=message)
        try:
            result = await build_result(await self._state(), self._context(), failure)
        except _CHECKPOINT_ERRORS:
            usage = await self._worker._store.usage(self._lease.investigation_id)
            result = InvestigationResult(
                investigation_id=self._lease.investigation_id,
                status="failed",
                prompt_version=PROMPT_VERSION,
                plan=None,
                evidence=[],
                diagnosis=None,
                failure=failure,
                usage=Usage(
                    llm_calls=usage.llm_calls,
                    tool_calls=0,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    duration_ms=round(
                        await self._worker._store.elapsed_ms(self._lease.investigation_id), 2
                    ),
                ),
            )
        await self._finish(result.model_copy(update={"status": "failed", "diagnosis": None}))
        await self._delete_checkpoints()

    async def _delete_checkpoints(self) -> None:
        try:
            await self._worker._checkpoints.adelete_thread(self._lease.investigation_id)
        except _DATABASE_ERRORS as exc:
            logger.warning(
                "could not delete checkpoints",
                extra={
                    "investigation_id": self._lease.investigation_id,
                    "error": type(exc).__name__,
                },
            )

    async def _heartbeat(self) -> None:
        """Renew the lease; cancel the work once it may have lapsed."""
        lease_seconds = self._worker._config.lease_seconds
        interval = lease_seconds / 3
        last_renewed = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            sent = time.monotonic()
            try:
                renewed: bool | None = await asyncio.wait_for(
                    self._worker._store.renew(self._lease, lease_seconds), timeout=interval
                )
            except (*_DATABASE_ERRORS, TimeoutError):
                renewed = None
            if renewed:
                last_renewed = sent
                continue
            # Stop a third of a lease before it could expire in the database, so
            # this worker has stopped before another can claim the investigation.
            if renewed is False or time.monotonic() - last_renewed >= lease_seconds * 2 / 3:
                logger.warning(
                    "lease lost; stopping",
                    extra={"investigation_id": self._lease.investigation_id},
                )
                self.interrupt(_Cause.LEASE_LOST)
                return
