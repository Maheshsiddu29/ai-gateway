"""Rows in the states a worker leaves them in, for journal and recovery tests."""

import hashlib
import json
import uuid
from typing import Any

from agent_runtime.investigation.graph import WORKFLOW_VERSION
from agent_runtime.investigation.prompts import PROMPT_VERSION
from agent_runtime.persistence.database import Connection
from agent_runtime.persistence.store import Lease

# A dollar per million input tokens, two per million output tokens.
PRICE = {
    "input_micro_usd_per_million_tokens": 1_000_000,
    "output_micro_usd_per_million_tokens": 2_000_000,
}
PRICING = {"planner": PRICE, "diagnosis": PRICE}


async def insert_running(
    conn: Connection,
    *,
    investigation_id: str | None = None,
    max_llm_calls: int = 2,
    fencing_token: int = 1,
    deadline_seconds: int = 3600,
    budget_micro_usd: int = 1_000_000,
    pricing: dict[str, Any] | None = PRICING,
) -> Lease:
    """An investigation that a worker has claimed, as a lease for that worker."""
    investigation_id = investigation_id or f"inv-{uuid.uuid4().hex}"
    await conn.execute(
        "INSERT INTO investigations (investigation_id, principal_id, request_hash, incident,"
        " workflow_version, prompt_version, max_llm_calls, max_tool_calls, max_output_tokens,"
        " budget_micro_usd, pricing, deadline_at) VALUES (%s, 'agent-key-0123456789ab', %s,"
        " 'Disk full.', %s, %s, %s, 6, 512, %s, %s, now() + make_interval(secs => %s))",
        (
            investigation_id,
            hashlib.sha256(investigation_id.encode()).digest(),
            WORKFLOW_VERSION,
            PROMPT_VERSION,
            max_llm_calls,
            budget_micro_usd,
            json.dumps(pricing) if pricing is not None else None,
            deadline_seconds,
        ),
    )
    await conn.execute(
        "UPDATE investigations SET status = 'running', fencing_token = %s, attempts = 1,"
        " lease_owner = 'worker-a', lease_expires_at = now() + interval '1 minute'"
        " WHERE investigation_id = %s",
        (fencing_token, investigation_id),
    )
    return Lease(investigation_id, "worker-a", fencing_token)


async def insert_accepted(
    conn: Connection,
    *,
    incident: str = "The database host is out of memory.",
    max_llm_calls: int = 2,
    deadline_seconds: float = 3600,
    budget_micro_usd: int = 1_000_000,
    pricing: dict[str, Any] | None = PRICING,
    workflow_version: str = WORKFLOW_VERSION,
) -> str:
    """A newly accepted investigation, as the API stores it."""
    investigation_id = f"inv-{uuid.uuid4().hex}"
    await conn.execute(
        "INSERT INTO investigations (investigation_id, principal_id, request_hash, incident,"
        " workflow_version, prompt_version, max_llm_calls, max_tool_calls, max_output_tokens,"
        " budget_micro_usd, pricing, deadline_at) VALUES (%s, 'agent-key-0123456789ab', %s, %s,"
        " %s, %s, %s, 6, 512, %s, %s, now() + make_interval(secs => %s))",
        (
            investigation_id,
            hashlib.sha256(investigation_id.encode()).digest(),
            incident,
            workflow_version,
            PROMPT_VERSION,
            max_llm_calls,
            budget_micro_usd,
            json.dumps(pricing) if pricing is not None else None,
            deadline_seconds,
        ),
    )
    return investigation_id


async def investigation(conn: Connection, investigation_id: str) -> dict[str, Any]:
    cursor = await conn.execute(
        "SELECT * FROM investigations WHERE investigation_id = %s", (investigation_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return row


async def operation_states(conn: Connection, investigation_id: str) -> list[tuple[str, str]]:
    cursor = await conn.execute(
        "SELECT operation_type, state FROM operations WHERE investigation_id = %s"
        " ORDER BY started_at",
        (investigation_id,),
    )
    return [(row["operation_type"], row["state"]) for row in await cursor.fetchall()]


async def checkpoint_rows(conn: Connection, investigation_id: str) -> int:
    cursor = await conn.execute(
        "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (investigation_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row["n"])
