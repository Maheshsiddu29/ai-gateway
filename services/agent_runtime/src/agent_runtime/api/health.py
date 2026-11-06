from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health")


@router.get("/live")
async def live() -> JSONResponse:
    """The process is running and the event loop is serving requests."""
    return JSONResponse({"status": "ok"})


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Startup has finished. The gateway is not contacted: probing it on every check
    would tie the runtime's readiness to the gateway's and add load to it."""
    if not request.app.state.ready:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
