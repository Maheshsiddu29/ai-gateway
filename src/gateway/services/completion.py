"""Model alias resolution and the completion request lifecycle.

The service is independent of HTTP. For each request it:

1. resolves the alias and builds the provider request, applying the model's output
   token ceiling, and lets the provider validate it;
2. obtains admission (when admission control is enabled), reserving the estimated
   input tokens plus the output allowance;
3. calls the provider within the request deadline;
4. settles the reservation with the usage recorded for every attempt, on every exit
   path including cancellation.

It emits one ``completion finished`` log event per request with the outcome.
"""

import asyncio
import dataclasses
import logging
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from dataclasses import dataclass

from gateway.admission.controller import AdmissionController
from gateway.admission.redis_store import Admission
from gateway.admission.tokens import estimate_input_tokens
from gateway.admission.usage import UsageLedger
from gateway.core.config import GatewayConfig
from gateway.core.metrics import Metrics
from gateway.core.security import Principal
from gateway.cost.session import CostSession
from gateway.cost.store import CostStore
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
from gateway.services.execution import Candidate, ProviderExecutor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    provider: str
    upstream_model: str
    default_max_tokens: int | None
    max_output_tokens: int | None
    # (provider, upstream model) pairs, in the order they are tried.
    fallbacks: tuple[tuple[str, str], ...] = ()


def routes_from_config(config: GatewayConfig) -> dict[str, ModelRoute]:
    return {
        alias: ModelRoute(
            alias=alias,
            provider=model.provider,
            upstream_model=model.model,
            default_max_tokens=model.default_max_tokens,
            max_output_tokens=model.max_output_tokens,
            fallbacks=tuple((target.provider, target.model) for target in model.fallbacks),
        )
        for alias, model in config.models.items()
    }


@dataclass(frozen=True)
class _Plan:
    route: ModelRoute
    # The primary first, then usable fallbacks. All share the primary's request
    # except for the upstream model.
    candidates: tuple[Candidate, ...]

    @property
    def request(self) -> ProviderRequest:
        return self.candidates[0].request


class CompletionService:
    def __init__(
        self,
        routes: Mapping[str, ModelRoute],
        providers: Mapping[str, Provider],
        executor: ProviderExecutor,
        metrics: Metrics,
        admission: AdmissionController | None = None,
        cost_store: CostStore | None = None,
    ) -> None:
        referenced = {
            provider
            for route in routes.values()
            for provider in [route.provider, *(name for name, _ in route.fallbacks)]
        }
        missing = referenced - providers.keys()
        if missing:
            raise ValueError(f"routes reference providers without adapters: {sorted(missing)}")
        self._routes = dict(routes)
        self._providers = dict(providers)
        self._executor = executor
        self._metrics = metrics
        self._admission = admission
        self._cost_store = cost_store

    async def complete(
        self, request: ChatCompletionRequest, principal: Principal, deadline: Deadline
    ) -> Completion:
        plan = self._plan(request)
        ledger = UsageLedger()
        trace = _CompletionTrace(principal, plan.route, ledger, self._metrics, stream=False)
        try:
            admission = await self._admit(principal, plan, deadline, trace)
            try:
                cost = self._cost_session(principal, plan)
                completion = await self._executor.complete(plan.candidates, deadline, ledger, cost)
            finally:
                await self._settle(admission, ledger)
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
        stream, settles the reservation, and is logged as a cancellation. The
        executor bounds each wait for the next upstream event by the deadline; the
        time a slow client takes to read is bounded by the response (see
        api.streaming).
        """
        plan = self._plan(request)
        ledger = UsageLedger()
        trace = _CompletionTrace(principal, plan.route, ledger, self._metrics, stream=True)
        end: StreamEnd | None = None
        try:
            admission = await self._admit(principal, plan, deadline, trace)
            try:
                cost = self._cost_session(principal, plan)
                events = self._executor.stream(plan.candidates, deadline, ledger, cost)
                async with aclosing(events):
                    async for event in events:
                        if isinstance(event, TextDelta):
                            trace.first_token()
                        else:
                            end = event
                        yield event
                if end is None:
                    raise InternalGatewayError("The provider stream ended unexpectedly.")
            finally:
                await self._settle(admission, ledger)
        except BaseException as exc:
            trace.failed(exc)
            raise
        trace.succeeded(end.finish_reason, end.usage, provider_response_id=None)

    def _plan(self, request: ChatCompletionRequest) -> _Plan:
        route = self._routes.get(request.model)
        if route is None:
            raise InvalidRequestError(
                "The requested model is not configured.",
                code="model_not_found",
                param="model",
                status_code=404,
            )
        primary = _provider_request(request, route)
        self._providers[route.provider].validate(primary)
        candidates = [Candidate(route.provider, primary)]
        for provider, model in route.fallbacks:
            fallback = dataclasses.replace(primary, model=model)
            try:
                self._providers[provider].validate(fallback)
            except GatewayError as exc:
                # A fallback that cannot run the request as given is skipped rather
                # than sent a modified request.
                logger.info(
                    "fallback skipped",
                    extra={
                        "model_alias": route.alias,
                        "fallback_provider": provider,
                        "error_code": exc.code,
                    },
                )
                continue
            candidates.append(Candidate(provider, fallback))
        return _Plan(route, tuple(candidates))

    async def _admit(
        self, principal: Principal, plan: _Plan, deadline: Deadline, trace: "_CompletionTrace"
    ) -> Admission | None:
        if self._admission is None:
            return None
        limits = self._admission.limits_for(principal)
        tokens = 0
        if limits.tokens is not None:
            if plan.request.max_tokens is None:
                raise InvalidRequestError(
                    "max_tokens is required because this key has a token rate limit"
                    " and the model has no output token limit.",
                    code="max_tokens_required",
                    param="max_tokens",
                )
            estimated_input = estimate_input_tokens(plan.request.messages)
            tokens = estimated_input + plan.request.max_tokens
            trace.reserved(estimated_input, tokens)
            if tokens > limits.tokens.capacity:
                raise InvalidRequestError(
                    "The request's token reservation (estimated input plus max_tokens)"
                    " exceeds this key's token rate limit.",
                    code="token_reservation_exceeds_limit",
                    param="max_tokens",
                )
        return await self._admission.admit(principal, limits, tokens, deadline)

    async def _settle(self, admission: Admission | None, ledger: UsageLedger) -> None:
        if self._admission is not None and admission is not None:
            await self._admission.settle(admission, ledger)

    def _cost_session(self, principal: Principal, plan: _Plan) -> CostSession | None:
        if self._cost_store is None:
            return None
        return CostSession(
            self._cost_store, principal.id, estimate_input_tokens(plan.request.messages)
        )


def _provider_request(request: ChatCompletionRequest, route: ModelRoute) -> ProviderRequest:
    return ProviderRequest(
        model=route.upstream_model,
        messages=tuple(request.messages),
        max_tokens=_output_allowance(request.max_tokens, route),
        temperature=request.temperature,
        top_p=request.top_p,
        stop=tuple(request.stop or ()),
        include_usage=request.include_usage,
    )


def _output_allowance(requested: int | None, route: ModelRoute) -> int | None:
    """The max_tokens sent upstream. It is always sent when the model has a ceiling,
    so the provider enforces the bound that admission reserved for."""
    ceiling = route.max_output_tokens
    if requested is not None and ceiling is not None and requested > ceiling:
        raise InvalidRequestError(
            f"max_tokens exceeds this model's limit of {ceiling}.",
            code="max_tokens_exceeds_limit",
            param="max_tokens",
        )
    if requested is not None:
        return requested
    return route.default_max_tokens if route.default_max_tokens is not None else ceiling


class _CompletionTrace:
    """Collects the fields of the ``completion finished`` log event.

    Prompts and generated text are never recorded.
    """

    def __init__(
        self,
        principal: Principal,
        route: ModelRoute,
        ledger: UsageLedger,
        metrics: Metrics,
        *,
        stream: bool,
    ) -> None:
        self._started = time.perf_counter()
        self._ledger = ledger
        self._metrics = metrics
        self._stream = stream
        self._fields: dict[str, object] = {
            "principal_id": principal.id,
            "model_alias": route.alias,
            "provider": route.provider,
            "upstream_model": route.upstream_model,
            "stream": stream,
        }

    def reserved(self, estimated_input_tokens: int, reserved_tokens: int) -> None:
        self._fields["estimated_input_tokens"] = estimated_input_tokens
        self._fields["reserved_tokens"] = reserved_tokens

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
        attempts = self._ledger.attempts
        if attempts:
            # The provider that made the last attempt, which served any response.
            self._fields["provider"] = attempts[-1].provider
        logger.log(
            level,
            "completion finished",
            extra={
                **self._fields,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "attempts": len(attempts),
            },
        )
