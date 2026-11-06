import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolClosed, PoolTimeout

from agent_runtime.persistence.store import InvestigationStore

router = APIRouter(prefix="/health")


@router.get("/live")
async def live() -> JSONResponse:
    """The process is running and the event loop is serving requests."""
    return JSONResponse({"status": "ok"})


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Startup has finished and the database answers. Investigations can only be
    accepted when they can be stored."""
    store: InvestigationStore | None = request.app.state.store
    if store is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    try:
        available = await store.ping()
    except (psycopg.Error, PoolTimeout, PoolClosed, OSError):
        available = False
    if not available:
        return JSONResponse({"status": "not_ready", "database": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})
