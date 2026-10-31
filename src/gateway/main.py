import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.api.errors import install_error_handlers
from gateway.api.middleware import RequestContextMiddleware
from gateway.api.routes import completions, health
from gateway.core.config import LoadedConfig, Settings
from gateway.core.security import StaticKeyAuthenticator
from gateway.providers.base import Provider
from gateway.services.completion import CompletionService, routes_from_config


def build_app(
    settings: Settings, config: LoadedConfig, providers: Mapping[str, Provider]
) -> FastAPI:
    routes = routes_from_config(config.gateway)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.completion_service = CompletionService(routes, providers)
        try:
            yield
        finally:
            app.state.completion_service = None
            await asyncio.gather(*(provider.aclose() for provider in providers.values()))

    # The interactive API docs are disabled: the request body is parsed manually
    # (see api.dependencies), so the generated schema would not describe it.
    app = FastAPI(
        title="AI Gateway",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.authenticator = StaticKeyAuthenticator(settings.api_keys)
    app.state.completion_service = None

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(completions.router)
    return app
