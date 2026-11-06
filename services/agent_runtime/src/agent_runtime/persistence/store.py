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
from agent_runtime.persistence.database import Connection, Pool


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
