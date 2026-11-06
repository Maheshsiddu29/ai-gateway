"""Application assembly.

``create_app`` is the ASGI entry point. ``build_app`` takes its dependencies
explicitly so tests can supply settings.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_runtime.api import health
from agent_runtime.api.errors import install_error_handlers
from agent_runtime.api.middleware import RequestContextMiddleware
from agent_runtime.config import Settings
from agent_runtime.logging import configure_logging
from agent_runtime.security import StaticKeyAuthenticator


def create_app() -> FastAPI:
    settings = Settings()
    configure_logging(settings.log_level)
    return build_app(settings)


def build_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False

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

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    return app
