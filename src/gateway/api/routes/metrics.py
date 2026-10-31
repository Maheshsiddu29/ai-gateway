from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.api.dependencies import PrincipalDep
from gateway.core.metrics import Metrics

router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request, principal: PrincipalDep) -> Response:
    """Prometheus metrics, protected by gateway API-key authentication."""
    registry: Metrics = request.app.state.metrics
    return Response(generate_latest(registry.registry), media_type=CONTENT_TYPE_LATEST)
