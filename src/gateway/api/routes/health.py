from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health")


@router.get("/live")
async def live() -> JSONResponse:
    """The process is running and the event loop is serving requests."""
    return JSONResponse({"status": "ok"})


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Startup completed and providers are initialized.

    Configuration is validated before the application is created, so a running
    application implies valid configuration. Providers are not contacted: probing
    upstream APIs on every readiness check would add cost and couple the gateway's
    availability to a single provider's.
    """
    if request.app.state.completion_service is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
