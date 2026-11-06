"""In-memory investigations: the investigation graph without persistence.

``Investigator`` runs the same LangGraph graph as the durable worker, with model
calls sent directly through the gateway client and nothing written to a
database. It keeps Phase 6A's rules (a model-call limit, no retries, one deadline
for the whole investigation) but survives nothing: a crash or restart loses the
investigation. The offline demonstration and the stage tests use it; the
service runs investigations in the durable worker.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from agent_runtime.diagnostics.tools import DiagnosticExecutor
from agent_runtime.gateway_client import ChatMessage, GatewayCallError, GatewayCompletion
from agent_runtime.investigation.graph import (
    STAGE_OF,
    CompiledGraph,
    InvestigationState,
    LlmFailure,
    LlmOutcome,
    LlmReply,
    ModelAliases,
    Node,
    OperationType,
    StageContext,
    UsageTotals,
    build_graph,
    build_result,
)
from agent_runtime.investigation.schemas import Failure, InvestigationResult

__all__ = [
    "CompletionClient",
    "InvestigationLimits",
    "Investigator",
    "ModelAliases",
    "new_investigation_id",
]

logger = logging.getLogger(__name__)

_GRAPH: CompiledGraph = build_graph().compile()


class CompletionClient(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        request_id: str | None = None,
    ) -> GatewayCompletion: ...


@dataclass(frozen=True)
class InvestigationLimits:
    max_llm_calls: int
    max_tool_calls: int
    max_output_tokens: int
    timeout_seconds: float


def new_investigation_id() -> str:
    return f"inv-{uuid.uuid4().hex}"


class DirectLlmCaller:
    """Gateway calls with a call limit and no journal. Never retries."""

    def __init__(
        self,
        gateway: CompletionClient,
        *,
        investigation_id: str,
        max_llm_calls: int,
        max_output_tokens: int,
    ) -> None:
        self._gateway = gateway
        self._investigation_id = investigation_id
        self._max_llm_calls = max_llm_calls
        self._max_output_tokens = max_output_tokens
        self._calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0

    async def call(
        self, operation: OperationType, model: str, messages: Sequence[ChatMessage]
    ) -> LlmOutcome:
        if self._calls >= self._max_llm_calls:
            return LlmFailure(
                "llm_call_limit_exceeded",
                f"The investigation reached its limit of {self._max_llm_calls} model calls.",
            )
        self._calls += 1
        try:
            completion = await self._gateway.complete(
                model=model,
                messages=messages,
                max_tokens=self._max_output_tokens,
                request_id=f"{self._investigation_id}-llm{self._calls}",
            )
        except GatewayCallError as exc:
            if exc.usage is not None:
                self._prompt_tokens += exc.usage.prompt_tokens
                self._completion_tokens += exc.usage.completion_tokens
            return LlmFailure(exc.code.value, exc.message, exc.status_code, exc.gateway_error_code)
        if completion.usage is not None:
            self._prompt_tokens += completion.usage.prompt_tokens
            self._completion_tokens += completion.usage.completion_tokens
        return LlmReply(completion.content)

    async def usage(self) -> UsageTotals:
        return UsageTotals(self._calls, self._prompt_tokens, self._completion_tokens)


class Investigator:
    def __init__(
        self,
        gateway: CompletionClient,
        diagnostics: DiagnosticExecutor,
        *,
        models: ModelAliases,
        limits: InvestigationLimits,
        allowed_paths: frozenset[str],
    ) -> None:
        self._gateway = gateway
        self._diagnostics = diagnostics
        self._models = models
        self._limits = limits
        self._allowed_paths = allowed_paths

    async def investigate(
        self, incident: str, *, investigation_id: str, principal_id: str | None = None
    ) -> InvestigationResult:
        started = time.perf_counter()
        current: list[Node] = ["plan"]
        finished: list[InvestigationResult] = []

        async def read_incident() -> str:
            return incident

        async def enter(node: Node) -> None:
            current[0] = node

        async def elapsed_ms() -> float:
            return (time.perf_counter() - started) * 1000

        async def finish(result: InvestigationResult) -> None:
            finished.append(result)

        async def admit_tool_calls(count: int) -> None:
            # validate_plan already holds the plan to max_tool_calls in memory.
            return None

        ctx = StageContext(
            investigation_id=investigation_id,
            llm=DirectLlmCaller(
                self._gateway,
                investigation_id=investigation_id,
                max_llm_calls=self._limits.max_llm_calls,
                max_output_tokens=self._limits.max_output_tokens,
            ),
            diagnostics=self._diagnostics,
            models=self._models,
            max_tool_calls=self._limits.max_tool_calls,
            allowed_paths=self._allowed_paths,
            incident=read_incident,
            enter=enter,
            elapsed_ms=elapsed_ms,
            finish=finish,
            admit_tool_calls=admit_tool_calls,
        )
        latest: InvestigationState = {}
        deadline = asyncio.timeout(self._limits.timeout_seconds)
        try:
            async with deadline:
                async for values in _GRAPH.astream({}, context=ctx, stream_mode="values"):
                    # stream_mode="values" yields the full graph state after each step.
                    latest = cast(InvestigationState, values)
        except asyncio.CancelledError:
            # The caller went away; the gateway call in flight, if any, is closed.
            logger.info(
                "investigation cancelled",
                extra={
                    "investigation_id": investigation_id,
                    "principal_id": principal_id,
                    "stage": STAGE_OF[current[0]].value,
                    "llm_calls": (await ctx.llm.usage()).llm_calls,
                    "tool_calls": len(latest.get("evidence", [])),
                },
            )
            raise
        except TimeoutError:
            if not deadline.expired():
                raise
            timeout = Failure(
                stage=STAGE_OF[current[0]],
                code="investigation_timeout",
                message=f"The investigation exceeded {self._limits.timeout_seconds} seconds.",
            )
            finished.append(await build_result(latest, ctx, timeout))
        result = finished[0]
        logger.info(
            "investigation finished",
            extra={
                "investigation_id": investigation_id,
                "principal_id": principal_id,
                "status": result.status,
                "failure_stage": result.failure.stage.value if result.failure else None,
                "failure_code": result.failure.code if result.failure else None,
                "conclusion": result.diagnosis.conclusion if result.diagnosis else None,
                "llm_calls": result.usage.llm_calls,
                "tool_calls": result.usage.tool_calls,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "duration_ms": result.usage.duration_ms,
            },
        )
        return result
