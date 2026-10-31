"""Model alias resolution and the completion request lifecycle.

The service is independent of HTTP: it receives a validated request and an
authenticated principal, dispatches to the provider configured for the alias, and
emits one ``completion finished`` log event per request with the outcome.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from gateway.core.config import GatewayConfig
from gateway.core.security import Principal
from gateway.errors import GatewayError, InvalidRequestError, ProviderError
from gateway.providers.base import Completion, Provider, ProviderRequest, Usage
from gateway.schemas.completions import ChatCompletionRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    provider: str
    upstream_model: str
    default_max_tokens: int | None


def routes_from_config(config: GatewayConfig) -> dict[str, ModelRoute]:
    return {
        alias: ModelRoute(
            alias=alias,
            provider=model.provider,
            upstream_model=model.model,
            default_max_tokens=model.default_max_tokens,
        )
        for alias, model in config.models.items()
    }


class CompletionService:
    def __init__(self, routes: Mapping[str, ModelRoute], providers: Mapping[str, Provider]) -> None:
        missing = {route.provider for route in routes.values()} - providers.keys()
        if missing:
            raise ValueError(f"routes reference providers without adapters: {sorted(missing)}")
        self._routes = dict(routes)
        self._providers = dict(providers)

    async def complete(self, request: ChatCompletionRequest, principal: Principal) -> Completion:
        route, provider = self._resolve(request.model)
        trace = _CompletionTrace(principal, route, stream=False)
        try:
            completion = await provider.complete(_provider_request(request, route))
        except BaseException as exc:
            trace.failed(exc)
            raise
        trace.succeeded(completion.finish_reason, completion.usage, completion.provider_response_id)
        return completion

    def _resolve(self, alias: str) -> tuple[ModelRoute, Provider]:
        route = self._routes.get(alias)
        if route is None:
            raise InvalidRequestError(
                "The requested model is not configured.",
                code="model_not_found",
                param="model",
                status_code=404,
            )
        return route, self._providers[route.provider]


def _provider_request(request: ChatCompletionRequest, route: ModelRoute) -> ProviderRequest:
    return ProviderRequest(
        model=route.upstream_model,
        messages=tuple(request.messages),
        max_tokens=request.max_tokens
        if request.max_tokens is not None
        else route.default_max_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        stop=tuple(request.stop or ()),
        include_usage=request.include_usage,
    )


class _CompletionTrace:
    """Collects the fields of the ``completion finished`` log event.

    Prompts and generated text are never recorded.
    """

    def __init__(self, principal: Principal, route: ModelRoute, *, stream: bool) -> None:
        self._started = time.perf_counter()
        self._fields: dict[str, object] = {
            "principal_id": principal.id,
            "model_alias": route.alias,
            "provider": route.provider,
            "upstream_model": route.upstream_model,
            "stream": stream,
        }

    def succeeded(
        self, finish_reason: str | None, usage: Usage | None, provider_response_id: str | None
    ) -> None:
        self._fields["finish_reason"] = finish_reason
        self._fields["provider_response_id"] = provider_response_id
        if usage is not None:
            self._fields["prompt_tokens"] = usage.prompt_tokens
            self._fields["completion_tokens"] = usage.completion_tokens
            self._fields["total_tokens"] = usage.total_tokens
        self._emit(logging.INFO, "success")

    def failed(self, exc: BaseException) -> None:
        if isinstance(exc, asyncio.CancelledError | GeneratorExit):
            self._emit(logging.INFO, "cancelled")
            return
        if isinstance(exc, GatewayError):
            self._fields["error_type"] = exc.error_type.value
            self._fields["error_code"] = exc.code
            if isinstance(exc, ProviderError):
                self._fields["upstream_status"] = exc.upstream_status
                self._fields["upstream_error_type"] = exc.upstream_error_type
            self._emit(logging.WARNING, "error")
            return
        self._fields["error_type"] = "internal_error"
        self._emit(logging.ERROR, "error")

    def _emit(self, level: int, outcome: str) -> None:
        duration_ms = round((time.perf_counter() - self._started) * 1000, 2)
        logger.log(
            level,
            "completion finished",
            extra={**self._fields, "outcome": outcome, "duration_ms": duration_ms},
        )
