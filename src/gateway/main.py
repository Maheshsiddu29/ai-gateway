"""Application assembly.

``create_app`` is the entry point used by the ASGI server. ``build_app`` takes its
dependencies explicitly so tests can supply configuration and provider transports.
"""

import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from gateway.api.errors import install_error_handlers
from gateway.api.middleware import RequestContextMiddleware
from gateway.api.routes import completions, health, metrics
from gateway.core.config import LoadedConfig, Settings, load_config
from gateway.core.logging import configure_logging
from gateway.core.metrics import Metrics
from gateway.core.security import StaticKeyAuthenticator
from gateway.providers.base import Provider
from gateway.providers.factory import ProviderFactory, build_provider
from gateway.services.completion import CompletionService, routes_from_config


def create_app() -> FastAPI:
    settings = Settings()
    configure_logging(settings.log_level)
    return build_app(settings, load_config(settings, os.environ))


def build_app(
    settings: Settings,
    config: LoadedConfig,
    provider_factory: ProviderFactory = build_provider,
) -> FastAPI:
    routes = routes_from_config(config.gateway)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            providers: dict[str, Provider] = {}
            for name, provider_config in config.gateway.providers.items():
                provider = provider_factory(name, provider_config, config.provider_api_keys[name])
                stack.push_async_callback(provider.aclose)
                providers[name] = provider
            app.state.completion_service = CompletionService(routes, providers)
            try:
                yield
            finally:
                app.state.completion_service = None

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
    app.state.metrics = Metrics()

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, metrics=app.state.metrics)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(completions.router)
    return app
