"""Model alias resolution and the completion request lifecycle.

The service is independent of HTTP: it receives a validated request and an
authenticated principal, dispatches to the provider configured for the alias, and
emits one ``completion finished`` log event per request with the outcome.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from dataclasses import dataclass

from gateway.core.config import GatewayConfig
from gateway.core.metrics import Metrics
from gateway.core.security import Principal
from gateway.errors import (
    DeadlineExceededError,
    GatewayError,
    InternalGatewayError,
    InvalidRequestError,
    ProviderError,
)
from gateway.providers.base import (
    Completion,
    Provider,
    ProviderRequest,
    StreamEnd,
    StreamEvent,
    TextDelta,
    Usage,
)
from gateway.resilience.deadline import Deadline
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
    def __init__(
        self,
        routes: Mapping[str, ModelRoute],
        providers: Mapping[str, Provider],
        metrics: Metrics,
    ) -> None:
        missing = {route.provider for route in routes.values()} - providers.keys()
        if missing:
            raise ValueError(f"routes reference providers without adapters: {sorted(missing)}")
        self._routes = dict(routes)
        self._providers = dict(providers)
        self._metrics = metrics

    async def complete(
        self, request: ChatCompletionRequest, principal: Principal, deadline: Deadline
    ) -> Completion:
        route, provider = self._resolve(request.model)
        trace = _CompletionTrace(principal, route, self._metrics, stream=False)
        try:
            async with deadline.limit():
                completion = await provider.complete(_provider_request(request, route))
        except BaseException as exc:
            trace.failed(exc)
            raise
        trace.succeeded(completion.finish_reason, completion.usage, completion.provider_response_id)
        return completion

    async def stream(
        self, request: ChatCompletionRequest, principal: Principal, deadline: Deadline
    ) -> AsyncGenerator[StreamEvent]:
        """Relay provider stream events, logging the outcome once the stream ends.

        Closing this generator early (client disconnect) closes the provider
        stream and is logged as a cancellation. The deadline bounds each wait for
        the next upstream event; the time a slow client takes to read is bounded by
        the response (see api.streaming).
        """
        route, provider = self._resolve(request.model)
        trace = _CompletionTrace(principal, route, self._metrics, stream=True)
        end: StreamEnd | None = None
        try:
            async with aclosing(provider.stream(_provider_request(request, route))) as events:
                while True:
                    async with deadline.limit():
                        event = await anext(events, None)
                    if event is None:
                        break
                    if isinstance(event, TextDelta):
                        trace.first_token()
                    else:
                        end = event
                    yield event
            if end is None:
                raise InternalGatewayError("The provider stream ended unexpectedly.")
        except BaseException as exc:
            trace.failed(exc)
            raise
        trace.succeeded(end.finish_reason, end.usage, provider_response_id=None)

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

    def __init__(
        self, principal: Principal, route: ModelRoute, metrics: Metrics, *, stream: bool
    ) -> None:
        self._started = time.perf_counter()
        self._metrics = metrics
        self._stream = stream
        self._fields: dict[str, object] = {
            "principal_id": principal.id,
            "model_alias": route.alias,
            "provider": route.provider,
            "upstream_model": route.upstream_model,
            "stream": stream,
        }

    def first_token(self) -> None:
        self._fields.setdefault(
            "time_to_first_token_ms", round((time.perf_counter() - self._started) * 1000, 2)
        )

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
        if isinstance(exc, DeadlineExceededError):
            self._metrics.deadline_exceeded.labels(str(self._stream).lower()).inc()
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
