"""The asynchronous investigation API.

``POST /v1/investigations`` stores the investigation and returns 202 once it is
committed; a durable worker runs it. ``GET /v1/investigations/{id}`` returns its
status and, when finished, its result. Callers see only their own
investigations; any other ID is answered exactly like an unknown one.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolClosed, PoolTimeout
from pydantic import BaseModel

from agent_runtime.api.dependencies import (
    IdempotencyKeyDep,
    InvestigationRequestDep,
    PrincipalDep,
    StoreDep,
)
from agent_runtime.api.errors import (
    ConflictError,
    NotFoundError,
    PersistenceUnavailableError,
    RateLimitedError,
)
from agent_runtime.config import ApiSettings
from agent_runtime.investigation.graph import WORKFLOW_VERSION
from agent_runtime.investigation.prompts import PROMPT_VERSION
from agent_runtime.investigation.schemas import InvestigationRequest, InvestigationResult
from agent_runtime.persistence.store import (
    ActiveLimitError,
    IdempotencyConflictError,
    InvestigationRecord,
    NewInvestigation,
)

router = APIRouter()

_INVESTIGATION_ID = re.compile(r"inv-[0-9a-f]{32}")
_DATABASE_ERRORS = (psycopg.Error, PoolTimeout, PoolClosed, OSError)


class Spending(BaseModel):
    """Estimated spending in micro-USD at the prices recorded on acceptance. Not an
    invoice: the gateway's ledger and the provider's bill are authoritative."""

    budget_micro_usd: int
    # Admitted calls not yet settled; for a reconciliation_required investigation,
    # the calls whose outcome is unknown.
    reserved_micro_usd: int
    settled_micro_usd: int


class InvestigationView(BaseModel):
    investigation_id: str
    status: str
    stage: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    spending: Spending
    result: InvestigationResult | None

    @classmethod
    def of(cls, record: InvestigationRecord) -> "InvestigationView":
        return cls(
            investigation_id=record.investigation_id,
            status=record.status,
            stage=record.current_stage,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
            spending=Spending(
                budget_micro_usd=record.budget_micro_usd,
                reserved_micro_usd=record.reserved_micro_usd,
                settled_micro_usd=record.settled_micro_usd,
            ),
            result=(
                InvestigationResult.model_validate(record.result)
                if record.result is not None
                else None
            ),
        )


def request_hash(body: InvestigationRequest) -> bytes:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest()


def _unavailable() -> PersistenceUnavailableError:
    return PersistenceUnavailableError("Investigations cannot be stored or read right now.")


# Dependencies resolve in declaration order, so the caller is authenticated before
# the body is read.
@router.post("/v1/investigations", status_code=202)
async def create_investigation(
    principal: PrincipalDep,
    store: StoreDep,
    idempotency_key: IdempotencyKeyDep,
    body: InvestigationRequestDep,
    request: Request,
) -> JSONResponse:
    """Accept an investigation. The response is sent only after it is committed;
    a client that disconnects afterwards does not affect it."""
    settings: ApiSettings = request.app.state.settings
    new = NewInvestigation(
        principal_id=principal.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash(body),
        incident=body.incident,
        workflow_version=WORKFLOW_VERSION,
        prompt_version=PROMPT_VERSION,
        max_llm_calls=settings.max_llm_calls,
        max_tool_calls=settings.max_tool_calls,
        max_output_tokens=settings.max_output_tokens,
        timeout_seconds=settings.investigation_timeout_seconds,
        budget_micro_usd=settings.max_investigation_cost_micro_usd,
        # The prices in force now stay with the investigation across restarts and
        # configuration changes.
        pricing={
            alias: settings.model_prices[alias].model_dump()
            for alias in (settings.planner_model, settings.diagnosis_model)
        },
    )
    try:
        record, created = await store.create(
            new, max_active_per_principal=settings.max_active_investigations_per_principal
        )
    except IdempotencyConflictError:
        raise ConflictError(
            "This Idempotency-Key was already used with a different request.",
            code="idempotency_key_reused",
            param="Idempotency-Key",
        ) from None
    except ActiveLimitError:
        raise RateLimitedError(
            "Too many unfinished investigations; retry when some have finished.",
            code="active_investigation_limit",
        ) from None
    except _DATABASE_ERRORS:
        raise _unavailable() from None
    headers: dict[str, Any] = {"Location": f"/v1/investigations/{record.investigation_id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(
        InvestigationView.of(record).model_dump(mode="json"), status_code=202, headers=headers
    )


@router.get("/v1/investigations/{investigation_id}")
async def get_investigation(
    investigation_id: str, principal: PrincipalDep, store: StoreDep
) -> JSONResponse:
    not_found = NotFoundError("Investigation not found.", code="investigation_not_found")
    if not _INVESTIGATION_ID.fullmatch(investigation_id):
        raise not_found
    try:
        record = await store.get(investigation_id, principal.id)
    except _DATABASE_ERRORS:
        raise _unavailable() from None
    if record is None:
        raise not_found
    return JSONResponse(InvestigationView.of(record).model_dump(mode="json"))
