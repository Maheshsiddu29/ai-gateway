"""Application assembly.

``create_app`` is the ASGI entry point. ``build_app`` takes its dependencies
explicitly so tests can supply settings and an HTTP transport for the gateway.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from agent_runtime.api import health
from agent_runtime.api.errors import install_error_handlers
from agent_runtime.api.middleware import RequestContextMiddleware
from agent_runtime.config import Settings
from agent_runtime.gateway_client import GatewayClient, GatewayTimeouts
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


def build_app(
    settings: Settings, gateway_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        gateway = build_gateway_client(settings, gateway_transport)
        app.state.gateway_client = gateway
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await gateway.aclose()

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

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    return app
