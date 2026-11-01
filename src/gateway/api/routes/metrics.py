from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.api.dependencies import MetricsReaderDep
from gateway.core.metrics import Metrics

router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request, reader: MetricsReaderDep) -> Response:
    """Prometheus metrics. Requires a metrics credential (GATEWAY_METRICS_API_KEYS);
    gateway API keys, which only grant inference and usage access, are refused."""
    registry: Metrics = request.app.state.metrics
    return Response(generate_latest(registry.registry), media_type=CONTENT_TYPE_LATEST)
