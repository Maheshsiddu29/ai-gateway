from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.admission.controller import AdmissionController

router = APIRouter(prefix="/health")


@router.get("/live")
async def live() -> JSONResponse:
    """The process is running and the event loop is serving requests."""
    return JSONResponse({"status": "ok"})


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Startup completed, providers are initialized, and Redis answers when admission
    control is enabled.

    Configuration is validated before the application is created, so a running
    application implies valid configuration. Providers are not contacted: probing
    upstream APIs on every readiness check would add cost and couple the gateway's
    availability to a single provider's. Redis is checked because, with admission
    control enabled, every completion request depends on it.
    """
    if request.app.state.completion_service is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    admission: AdmissionController | None = request.app.state.admission
    cost_store = request.app.state.cost_store
    if cost_store is not None and not await cost_store.ping():
        return JSONResponse({"status": "not_ready", "cost": "unavailable"}, status_code=503)
    if admission is None:
        return JSONResponse({"status": "ready", "admission": "disabled"})
    if not await admission.is_ready():
        return JSONResponse({"status": "not_ready", "admission": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ready", "admission": "enabled"})
