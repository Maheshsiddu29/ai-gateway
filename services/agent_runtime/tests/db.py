"""Rows in the states a worker leaves them in, for journal and recovery tests."""

import hashlib
import uuid

from agent_runtime.investigation.graph import WORKFLOW_VERSION
from agent_runtime.investigation.prompts import PROMPT_VERSION
from agent_runtime.persistence.database import Connection
from agent_runtime.persistence.store import Lease


async def insert_running(
    conn: Connection,
    *,
    investigation_id: str | None = None,
    max_llm_calls: int = 2,
    fencing_token: int = 1,
    deadline_seconds: int = 3600,
) -> Lease:
    """An investigation that a worker has claimed, as a lease for that worker."""
    investigation_id = investigation_id or f"inv-{uuid.uuid4().hex}"
    await conn.execute(
        "INSERT INTO investigations (investigation_id, principal_id, request_hash, incident,"
        " workflow_version, prompt_version, max_llm_calls, max_tool_calls, max_output_tokens,"
        " deadline_at) VALUES (%s, 'agent-key-0123456789ab', %s, 'Disk full.', %s, %s, %s,"
        " 6, 512, now() + make_interval(secs => %s))",
        (
            investigation_id,
            hashlib.sha256(investigation_id.encode()).digest(),
            WORKFLOW_VERSION,
            PROMPT_VERSION,
            max_llm_calls,
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
