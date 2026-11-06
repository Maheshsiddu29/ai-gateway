"""Application assembly for the investigation API.

``create_app`` is the ASGI entry point. ``build_app`` takes its dependencies
explicitly so tests can supply settings and a connection pool. The API stores and
reads investigations; the durable worker (``python -m agent_runtime.worker``)
runs them.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_runtime.api import health, investigations
from agent_runtime.api.errors import install_error_handlers
from agent_runtime.api.middleware import RequestContextMiddleware
from agent_runtime.config import ApiSettings
from agent_runtime.logging import configure_logging
from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import Pool, create_pool
from agent_runtime.persistence.store import InvestigationStore
from agent_runtime.security import StaticKeyAuthenticator


def create_app() -> FastAPI:
    settings = ApiSettings()
    configure_logging(settings.log_level)
    return build_app(settings)


def build_app(settings: ApiSettings, pool: Pool | None = None) -> FastAPI:
    """``pool``, when given, is used as is and not closed; otherwise one is opened."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = pool is None
        active = pool or create_pool(
            settings.database_url,
            settings.database_schema,
            "agent-runtime-api",
            max_size=settings.database_pool_size,
            timeout=settings.database_timeout_seconds,
        )
        if owned:
            await active.open(wait=True, timeout=settings.database_timeout_seconds)
        try:
            async with active.connection() as conn:
                await migrations.verify(conn)
            app.state.store = InvestigationStore(active)
            yield
        finally:
            app.state.store = None
            if owned:
                await active.close()

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
    app.state.store = None

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(investigations.router)
    return app
