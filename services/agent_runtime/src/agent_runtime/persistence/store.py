"""PostgreSQL access for investigations and their model call journal.

Every write that advances an investigation runs in one transaction that first
locks the investigation row and checks the caller's ``Lease``: the investigation
must be ``running`` with the lease's fencing token. A worker whose lease was taken
over therefore changes nothing (``LeaseLostError``). The database's constraints and
triggers enforce the state machines independently of this code.
"""

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, LiteralString

from psycopg import sql

from agent_runtime.investigation.graph import OperationType, UsageTotals
from agent_runtime.investigation.schemas import InvestigationResult
from agent_runtime.persistence.database import Connection, Pool

_CLAIM_LOCK = "agent_runtime.claim"


class OperationState(StrEnum):
    INTENT = "intent"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    ABANDONED = "abandoned"


class LeaseLostError(Exception):
    """The investigation is no longer owned by this lease; nothing was written."""


class OperationConflictError(Exception):
    """The operation was not in the state the transition requires."""


class AdmissionRefusedError(Exception):
    """A limit of the investigation does not allow another operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Lease:
    investigation_id: str
    worker_id: str
    fencing_token: int


@dataclass(frozen=True)
class Operation:
    operation_id: uuid.UUID
    operation_type: OperationType
    state: OperationState
    model_alias: str
    gateway_request_id: str
    max_tokens: int
    reply: str | None
    failure_code: str | None
    may_have_executed: bool | None
    gateway_status: int | None
    gateway_error_code: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Operation":
        return cls(
            operation_id=row["operation_id"],
            operation_type=OperationType(row["operation_type"]),
            state=OperationState(row["state"]),
            model_alias=row["model_alias"],
            gateway_request_id=row["gateway_request_id"],
            max_tokens=row["max_tokens"],
            reply=row["reply"],
            failure_code=row["failure_code"],
            may_have_executed=row["may_have_executed"],
            gateway_status=row["gateway_status"],
            gateway_error_code=row["gateway_error_code"],
        )


@dataclass(frozen=True)
class Claim:
    """An investigation a worker now owns, with the limits recorded when it was accepted."""

    lease: Lease
    attempts: int
    remaining_seconds: float
    max_tool_calls: int
    max_output_tokens: int


@dataclass(frozen=True)
class ObservedUsage:
    prompt_tokens: int
    completion_tokens: int

    def to_json(self) -> str:
        return json.dumps(
            {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}
        )


async def _fence(conn: Connection, lease: Lease) -> dict[str, Any]:
    """Lock the investigation row and check that ``lease`` still owns it."""
    cursor = await conn.execute(
        "SELECT * FROM investigations WHERE investigation_id = %s FOR UPDATE",
        (lease.investigation_id,),
    )
    row = await cursor.fetchone()
    if row is None or row["status"] != "running" or row["fencing_token"] != lease.fencing_token:
        raise LeaseLostError(lease.investigation_id)
    return row


class InvestigationStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    # Ownership --------------------------------------------------------------------

    async def claim(
        self, worker_id: str, *, lease_seconds: float, max_running: int, workflow_version: str
    ) -> Claim | None:
        """Take the oldest claimable investigation, or return None.

        Claimable: ``accepted``, or ``running`` with an expired or released lease,
        and written by this workflow version. Claims are serialized by a
        transaction-scoped advisory lock, so ``max_running`` live leases are never
        exceeded across workers. Each claim increments the fencing token.
        """
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_CLAIM_LOCK,)
            )
            cursor = await conn.execute(
                "SELECT count(*) AS live FROM investigations"
                " WHERE status = 'running' AND lease_expires_at > now()"
            )
            row = await cursor.fetchone()
            if row is None or row["live"] >= max_running:
                return None
            cursor = await conn.execute(
                "UPDATE investigations SET status = 'running', lease_owner = %s,"
                " lease_expires_at = now() + make_interval(secs => %s),"
                " fencing_token = fencing_token + 1, attempts = attempts + 1"
                " WHERE investigation_id = ("
                "   SELECT investigation_id FROM investigations"
                "   WHERE workflow_version = %s AND (status = 'accepted' OR (status = 'running'"
                "     AND (lease_expires_at IS NULL OR lease_expires_at <= now())))"
                "   ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
                " RETURNING investigation_id, fencing_token, attempts, max_tool_calls,"
                " max_output_tokens, extract(epoch FROM deadline_at - now()) AS remaining",
                (worker_id, lease_seconds, workflow_version),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return Claim(
            Lease(row["investigation_id"], worker_id, row["fencing_token"]),
            attempts=row["attempts"],
            remaining_seconds=float(row["remaining"]),
            max_tool_calls=row["max_tool_calls"],
            max_output_tokens=row["max_output_tokens"],
        )

    async def renew(self, lease: Lease, lease_seconds: float) -> bool:
        """Extend the lease from database time; False if it is no longer this worker's."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE investigations SET lease_expires_at = now() + make_interval(secs => %s)"
                " WHERE investigation_id = %s AND fencing_token = %s AND status = 'running'"
                " AND lease_owner = %s",
                (lease_seconds, lease.investigation_id, lease.fencing_token, lease.worker_id),
            )
            return cursor.rowcount == 1

    async def release(self, lease: Lease) -> None:
        """Give the investigation back unfinished, so another worker can resume it now."""
        async with self._pool.connection() as conn, conn.transaction():
            await _fence(conn, lease)
            await conn.execute(
                "UPDATE investigations SET lease_owner = NULL, lease_expires_at = NULL"
                " WHERE investigation_id = %s",
                (lease.investigation_id,),
            )

    async def enter_stage(self, lease: Lease, stage: str) -> float:
        """Record the stage being entered; returns seconds left before the deadline."""
        async with self._pool.connection() as conn, conn.transaction():
            await _fence(conn, lease)
            cursor = await conn.execute(
                "UPDATE investigations SET current_stage = %s WHERE investigation_id = %s"
                " RETURNING extract(epoch FROM deadline_at - now()) AS remaining",
                (stage, lease.investigation_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LeaseLostError(lease.investigation_id)
        return float(row["remaining"])

    async def incident(self, lease: Lease) -> str:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT incident FROM investigations WHERE investigation_id = %s"
                " AND fencing_token = %s AND status = 'running'",
                (lease.investigation_id, lease.fencing_token),
            )
            row = await cursor.fetchone()
        if row is None or row["incident"] is None:
            raise LeaseLostError(lease.investigation_id)
        return str(row["incident"])

    async def elapsed_ms(self, investigation_id: str) -> float:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT extract(epoch FROM now() - created_at) * 1000 AS elapsed"
                " FROM investigations WHERE investigation_id = %s",
                (investigation_id,),
            )
            row = await cursor.fetchone()
        return 0.0 if row is None else float(row["elapsed"])

    async def finish(self, lease: Lease, result: InvestigationResult) -> None:
        """Make the investigation terminal with ``result``, in one fenced transaction.

        Intents that were never dispatched are abandoned, and the incident text and
        recorded model replies are deleted. The terminal row cannot change again.
        """
        failure_code = result.failure.code if result.failure is not None else None
        async with self._pool.connection() as conn, conn.transaction():
            await _fence(conn, lease)
            await conn.execute(
                "UPDATE operations SET state = 'abandoned', completed_at = now(),"
                " may_have_executed = false WHERE investigation_id = %s AND state = 'intent'",
                (lease.investigation_id,),
            )
            await conn.execute(
                "UPDATE operations SET reply = NULL WHERE investigation_id = %s"
                " AND reply IS NOT NULL",
                (lease.investigation_id,),
            )
            await conn.execute(
                "UPDATE investigations SET status = %s, failure_code = %s, result = %s,"
                " incident = NULL, completed_at = now(), current_stage = 'finalize',"
                " lease_owner = NULL, lease_expires_at = NULL WHERE investigation_id = %s",
                (result.status, failure_code, result.model_dump_json(), lease.investigation_id),
            )

    # The model call journal ------------------------------------------------------

    async def latest_operation(
        self, investigation_id: str, operation_type: OperationType
    ) -> Operation | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM operations WHERE investigation_id = %s AND operation_type = %s"
                " ORDER BY attempt DESC LIMIT 1",
                (investigation_id, operation_type.value),
            )
            row = await cursor.fetchone()
        return None if row is None else Operation.from_row(row)

    async def create_intent(
        self, lease: Lease, operation_type: OperationType, model_alias: str, max_tokens: int
    ) -> Operation:
        """Admit one model call against the investigation's limits and record its intent.

        Raises ``AdmissionRefusedError`` when a limit is reached; nothing is written then.
        """
        async with self._pool.connection() as conn, conn.transaction():
            investigation = await _fence(conn, lease)
            if investigation["llm_calls_admitted"] >= investigation["max_llm_calls"]:
                raise AdmissionRefusedError(
                    "llm_call_limit_exceeded",
                    "The investigation reached its limit of"
                    f" {investigation['max_llm_calls']} model calls.",
                )
            await conn.execute(
                "UPDATE investigations SET llm_calls_admitted = llm_calls_admitted + 1"
                " WHERE investigation_id = %s",
                (lease.investigation_id,),
            )
            cursor = await conn.execute(
                "INSERT INTO operations (operation_id, investigation_id, operation_type,"
                " attempt, state, fencing_token, model_alias, gateway_request_id, max_tokens)"
                " VALUES (%s, %s, %s, 1, 'intent', %s, %s, %s, %s) RETURNING *",
                (
                    uuid.uuid4(),
                    lease.investigation_id,
                    operation_type.value,
                    lease.fencing_token,
                    model_alias,
                    f"{lease.investigation_id}-{operation_type.value}-1",
                    max_tokens,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING returned no row")
        return Operation.from_row(row)

    async def mark_dispatched(self, lease: Lease, operation: Operation) -> None:
        """Commit that the request is about to be sent. Must precede the HTTP request."""
        await self._transition(
            lease,
            operation,
            OperationState.INTENT,
            OperationState.DISPATCHED,
            "dispatched_at = now()",
        )

    async def record_success(
        self, lease: Lease, operation: Operation, reply: str, usage: ObservedUsage | None
    ) -> None:
        await self._transition(
            lease,
            operation,
            OperationState.DISPATCHED,
            OperationState.SUCCEEDED,
            "completed_at = now(), may_have_executed = true, reply = %s, usage = %s",
            (reply, usage.to_json() if usage is not None else None),
        )

    async def record_failure(
        self,
        lease: Lease,
        operation: Operation,
        *,
        code: str,
        may_have_executed: bool,
        gateway_status: int | None = None,
        gateway_error_code: str | None = None,
        usage: ObservedUsage | None = None,
    ) -> None:
        await self._transition(
            lease,
            operation,
            OperationState.DISPATCHED,
            OperationState.FAILED,
            "completed_at = now(), failure_code = %s, may_have_executed = %s,"
            " gateway_status = %s, gateway_error_code = %s, usage = %s",
            (
                code,
                may_have_executed,
                gateway_status,
                gateway_error_code,
                usage.to_json() if usage is not None else None,
            ),
        )

    async def mark_uncertain(self, lease: Lease, operation: Operation) -> None:
        """A dispatched call without a recorded outcome: it may have run, so it is
        never sent again."""
        await self._transition(
            lease,
            operation,
            OperationState.DISPATCHED,
            OperationState.UNCERTAIN,
            "completed_at = now(), may_have_executed = true",
        )

    async def usage(self, investigation_id: str) -> UsageTotals:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) FILTER (WHERE state NOT IN ('intent', 'abandoned')) AS calls,"
                " coalesce(sum((usage->>'prompt_tokens')::bigint), 0) AS prompt_tokens,"
                " coalesce(sum((usage->>'completion_tokens')::bigint), 0) AS completion_tokens"
                " FROM operations WHERE investigation_id = %s",
                (investigation_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return UsageTotals(0, 0, 0)
        return UsageTotals(row["calls"], row["prompt_tokens"], row["completion_tokens"])

    async def _transition(
        self,
        lease: Lease,
        operation: Operation,
        expected: OperationState,
        new: OperationState,
        assignments: LiteralString,
        params: tuple[object, ...] = (),
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await _fence(conn, lease)
            statement = sql.SQL(
                "UPDATE operations SET state = %s, {} WHERE operation_id = %s AND state = %s"
            ).format(sql.SQL(assignments))
            cursor = await conn.execute(
                statement, (new.value, *params, operation.operation_id, expected.value)
            )
            if cursor.rowcount != 1:
                raise OperationConflictError(
                    f"operation {operation.operation_id} is not {expected.value}"
                )
