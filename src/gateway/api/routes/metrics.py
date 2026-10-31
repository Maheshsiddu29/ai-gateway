from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.core.metrics import Metrics

router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus metrics. Unauthenticated, like the health endpoints; expose it only
    on networks trusted to see request counts and provider error rates."""
    registry: Metrics = request.app.state.metrics
    return Response(generate_latest(registry.registry), media_type=CONTENT_TYPE_LATEST)
