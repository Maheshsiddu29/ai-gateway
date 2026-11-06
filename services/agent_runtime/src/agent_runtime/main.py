"""Application assembly.

``create_app`` is the ASGI entry point. ``build_app`` takes its dependencies
explicitly so tests can supply settings, an HTTP transport for the gateway, and a
diagnostic backend.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from agent_runtime.api import health, investigations
from agent_runtime.api.errors import install_error_handlers
from agent_runtime.api.middleware import RequestContextMiddleware
from agent_runtime.config import Settings
from agent_runtime.diagnostics.local import LocalDiagnostics
from agent_runtime.diagnostics.tools import DiagnosticBackend, DiagnosticExecutor
from agent_runtime.gateway_client import GatewayClient, GatewayTimeouts
from agent_runtime.investigation.workflow import InvestigationLimits, Investigator, ModelAliases
from agent_runtime.logging import configure_logging
from agent_runtime.security import StaticKeyAuthenticator


def create_app() -> FastAPI:
    settings = Settings()
    configure_logging(settings.log_level)
    return build_app(settings)


def build_gateway_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
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


def build_investigator(
    settings: Settings, gateway: GatewayClient, backend: DiagnosticBackend
) -> Investigator:
    return Investigator(
        gateway,
        DiagnosticExecutor(
            backend,
            timeout_seconds=settings.tool_timeout_seconds,
            max_output_bytes=settings.max_tool_output_bytes,
        ),
        models=ModelAliases(planner=settings.planner_model, diagnosis=settings.diagnosis_model),
        limits=InvestigationLimits(
            max_llm_calls=settings.max_llm_calls,
            max_tool_calls=settings.max_tool_calls,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.investigation_timeout_seconds,
        ),
        allowed_paths=frozenset(settings.filesystem_paths),
    )


def build_app(
    settings: Settings,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
    diagnostic_backend: DiagnosticBackend | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        local: LocalDiagnostics | None = None
        backend = diagnostic_backend
        if backend is None:
            backend = local = LocalDiagnostics(
                settings.procfs_root, settings.cgroup_root, workers=settings.diagnostic_workers
            )
        gateway = build_gateway_client(settings, gateway_transport)
        app.state.gateway_client = gateway
        app.state.investigator = build_investigator(settings, gateway, backend)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            app.state.investigator = None
            await gateway.aclose()
            if local is not None:
                local.close()

    # Request bodies are parsed manually with a size limit, so a generated schema
    # would not describe them; the interactive docs are disabled.
    app = FastAPI(
        title="Agent Runtime",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.authenticator = StaticKeyAuthenticator(settings.api_keys)
    app.state.ready = False
    app.state.gateway_client = None
    app.state.investigator = None
    app.state.investigation_capacity = asyncio.Semaphore(settings.max_concurrent_investigations)

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(investigations.router)
    return app
