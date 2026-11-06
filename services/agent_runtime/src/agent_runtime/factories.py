"""Construction of the clients and executors shared by the API and the worker."""

import httpx

from agent_runtime.config import DiagnosticsFields, GatewayFields
from agent_runtime.diagnostics.tools import DiagnosticBackend, DiagnosticExecutor
from agent_runtime.gateway_client import GatewayClient, GatewayTimeouts


def build_gateway_client(
    settings: GatewayFields, transport: httpx.AsyncBaseTransport | None = None
) -> GatewayClient:
    return GatewayClient(
        str(settings.gateway_url),
        settings.gateway_api_key,
        timeouts=GatewayTimeouts(
            connect=settings.gateway_connect_timeout_seconds,
            read=settings.gateway_read_timeout_seconds,
            write=settings.gateway_write_timeout_seconds,
            pool=settings.gateway_pool_timeout_seconds,
        ),
        max_connections=settings.gateway_max_connections,
        max_request_bytes=settings.gateway_max_request_bytes,
        max_response_bytes=settings.gateway_max_response_bytes,
        transport=transport,
    )


def build_diagnostic_executor(
    settings: DiagnosticsFields, backend: DiagnosticBackend
) -> DiagnosticExecutor:
    return DiagnosticExecutor(
        backend,
        timeout_seconds=settings.tool_timeout_seconds,
        max_output_bytes=settings.max_tool_output_bytes,
    )
