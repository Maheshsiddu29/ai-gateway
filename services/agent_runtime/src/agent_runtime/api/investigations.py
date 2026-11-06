import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_runtime.api.dependencies import InvestigationRequestDep, InvestigatorDep, PrincipalDep
from agent_runtime.api.disconnect import run_until_disconnect
from agent_runtime.api.errors import RateLimitedError
from agent_runtime.investigation.workflow import new_investigation_id

router = APIRouter()


# Dependencies resolve in declaration order, so the caller is authenticated before
# the body is read and before any capacity is taken.
@router.post("/v1/investigations")
async def create_investigation(
    principal: PrincipalDep,
    investigator: InvestigatorDep,
    body: InvestigationRequestDep,
    request: Request,
) -> JSONResponse:
    """Run one investigation to completion and return its result.

    Every investigation that starts returns 200, whether it completed or failed;
    the result's ``status`` and ``failure`` say which. Error statuses are only for
    requests that never started an investigation. If the client disconnects, the
    investigation is cancelled.
    """
    capacity: asyncio.Semaphore = request.app.state.investigation_capacity
    if capacity.locked():
        raise RateLimitedError(
            "Too many investigations are running; retry later.",
            code="concurrency_limit_exceeded",
        )
    async with capacity:
        result = await run_until_disconnect(
            request.receive,
            investigator.investigate(
                body.incident, investigation_id=new_investigation_id(), principal_id=principal.id
            ),
        )
    return JSONResponse(result.model_dump(mode="json"))
