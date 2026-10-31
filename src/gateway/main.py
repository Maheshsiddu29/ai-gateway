"""Application assembly.

``create_app`` is the entry point used by the ASGI server. ``build_app`` takes its
dependencies explicitly so tests can supply configuration and provider transports.
"""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from gateway.admission.controller import AdmissionController
from gateway.admission.redis_store import RedisAdmissionStore
from gateway.api.errors import install_error_handlers
from gateway.api.middleware import RequestContextMiddleware
from gateway.api.routes import completions, health, metrics, usage
from gateway.core.config import (
    GatewayConfig,
    LoadedConfig,
    Settings,
    load_config,
    require_redis_url,
)
from gateway.core.logging import configure_logging
from gateway.core.metrics import Metrics
from gateway.core.redis import create_redis_client
from gateway.core.security import StaticKeyAuthenticator
from gateway.cost.store import CostStore
from gateway.providers.base import Provider
from gateway.providers.factory import ProviderFactory, build_provider
from gateway.services.completion import CompletionService, routes_from_config
from gateway.services.execution import build_executor

logger = logging.getLogger(__name__)


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
            admission = await _start_admission(stack, settings, config.gateway, app.state.metrics)
            app.state.admission = admission
            cost_store = None
            if config.gateway.cost.enabled:
                if settings.database_url is None:
                    raise RuntimeError("GATEWAY_DATABASE_URL is required for cost accounting")
                cost_store = CostStore.from_url(
                    settings.database_url.get_secret_value(),
                    config.gateway.cost,
                    reservation_ttl=(
                        math.ceil(config.gateway.resilience.request_deadline_seconds) + 60
                    ),
                    metrics=app.state.metrics,
                )
                stack.push_async_callback(cost_store.close)
                if not await cost_store.ping():
                    raise RuntimeError("cost accounting database is unavailable or not migrated")
                await cost_store.recover_expired()
                recovery_task = asyncio.create_task(_recover_costs(cost_store))
                stack.push_async_callback(_stop_recovery, recovery_task)
            app.state.cost_store = cost_store
            app.state.completion_service = CompletionService(
                routes,
                providers,
                build_executor(providers, config.gateway.resilience, app.state.metrics),
                app.state.metrics,
                admission,
                cost_store,
            )
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
    app.state.admission = None
    app.state.cost_store = None
    app.state.metrics = Metrics()
    app.state.resilience = config.gateway.resilience
    app.state.cost_config = config.gateway.cost

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, metrics=app.state.metrics)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(completions.router)
    app.include_router(usage.router)
    return app


async def _recover_costs(store: CostStore) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await store.recover_expired()
        except SQLAlchemyError as exc:
            logger.error("cost reservation recovery failed: %s", type(exc).__name__)


async def _stop_recovery(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _start_admission(
    stack: AsyncExitStack, settings: Settings, config: GatewayConfig, metrics: Metrics
) -> AdmissionController | None:
    if not config.admission.enabled:
        logger.warning(
            "admission control is disabled: no request, token, or concurrency limits are enforced"
        )
        return None
    redis = create_redis_client(
        require_redis_url(settings), config.admission, settings.redis_ca_file
    )
    stack.push_async_callback(redis.aclose, close_connection_pool=True)
    controller = AdmissionController(
        RedisAdmissionStore(redis),
        config.admission,
        request_deadline_seconds=config.resilience.request_deadline_seconds,
        metrics=metrics,
    )
    # Registered after the client so that it runs first: pending settlements finish
    # before the connection pool is closed.
    stack.push_async_callback(controller.aclose)
    return controller
