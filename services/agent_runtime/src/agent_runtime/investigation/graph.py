"""The investigation as a LangGraph ``StateGraph``.

Six nodes, each with typed inputs and outputs:

    plan -> validate_plan -> collect_diagnostics -> diagnose -> validate_diagnosis -> finalize

Any node can set ``failure``, which routes straight to ``finalize``. The stage
logic is Phase 6A's: the same parsers, tool allowlist, and evidence checks.

Graph state holds only JSON values, so a checkpoint never needs to construct an
object, and every value a node reads from state is validated again, so a
corrupted or tampered checkpoint ends the investigation instead of steering it.
The incident text is not part of the state; nodes read it through the context.

Everything with side effects comes from ``StageContext``: model calls (journaled
in the durable worker, direct in the in-memory runner), diagnostics, and the
terminal write. Nodes may run again after a crash: the validators are pure,
diagnostics only read, and model calls are protected by the caller's journal.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError

from agent_runtime.diagnostics.tools import (
    DiagnosticExecutor,
    DiagnosticStatus,
    ToolRequest,
    ToolRequestError,
    parse_tool_request,
)
from agent_runtime.gateway_client import ChatMessage
from agent_runtime.investigation.prompts import (
    PROMPT_VERSION,
    diagnosis_messages,
    planning_messages,
)
from agent_runtime.investigation.schemas import (
    Diagnosis,
    Evidence,
    Failure,
    InvestigationResult,
    Plan,
    PlannedTool,
    Stage,
    Usage,
)

# Changes whenever the graph's nodes, edges, or state change incompatibly. An
# investigation is only resumed by a runtime with the same version.
WORKFLOW_VERSION = "linux-investigation-graph-1"

# One optional Markdown code fence around the whole reply. Nothing else is
# stripped or repaired before strict parsing.
_FENCE = re.compile(r"```(?:json)?[ \t]*\n(?P<body>.*)\n[ \t]*```", re.DOTALL)

type Node = Literal[
    "plan", "validate_plan", "collect_diagnostics", "diagnose", "validate_diagnosis", "finalize"
]
NODES: tuple[Node, ...] = (
    "plan",
    "validate_plan",
    "collect_diagnostics",
    "diagnose",
    "validate_diagnosis",
    "finalize",
)
STAGE_OF: dict[Node, Stage] = {
    "plan": Stage.PLANNING,
    "validate_plan": Stage.PLANNING,
    "collect_diagnostics": Stage.DIAGNOSTICS,
    "diagnose": Stage.DIAGNOSIS,
    "validate_diagnosis": Stage.DIAGNOSIS,
    "finalize": Stage.DIAGNOSIS,
}


class OperationType(StrEnum):
    PLAN = "plan"
    DIAGNOSE = "diagnose"


class InvestigationState(TypedDict, total=False):
    plan_reply: str
    plan: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    diagnosis_reply: str
    diagnosis: dict[str, Any]
    failure: dict[str, Any]
    # Set with ``failure`` when a model call's outcome is unknown.
    reconciliation_required: bool


@dataclass(frozen=True)
class LlmReply:
    content: str


@dataclass(frozen=True)
class LlmFailure:
    code: str
    message: str
    gateway_status: int | None = None
    gateway_error_code: str | None = None
    # The call may have run and its outcome is unknown; nothing may repeat it.
    reconciliation_required: bool = False


type LlmOutcome = LlmReply | LlmFailure


@dataclass(frozen=True)
class UsageTotals:
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int


class LlmCaller(Protocol):
    async def call(
        self, operation: OperationType, model: str, messages: Sequence[ChatMessage]
    ) -> LlmOutcome: ...

    async def usage(self) -> UsageTotals: ...


@dataclass(frozen=True)
class ModelAliases:
    planner: str
    diagnosis: str


type ResultStatus = Literal["completed", "failed", "reconciliation_required"]


@dataclass(frozen=True)
class StageContext:
    investigation_id: str
    llm: LlmCaller
    diagnostics: DiagnosticExecutor
    models: ModelAliases
    max_tool_calls: int
    allowed_paths: frozenset[str]
    incident: Callable[[], Awaitable[str]]
    # Called before every node. The durable worker checks its lease, deadline,
    # and shutdown here and records the stage; it may raise to stop the graph.
    enter: Callable[[Node], Awaitable[None]]
    elapsed_ms: Callable[[], Awaitable[float]]
    # Persists the result and the terminal status. Called once, by finalize.
    finish: Callable[[InvestigationResult], Awaitable[None]]


class CheckpointStateError(Exception):
    """A value read from graph state does not have the shape this graph writes."""


class StageFailedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def _failure(node: Node, code: str, message: str, **gateway: Any) -> dict[str, Any]:
    return Failure(stage=STAGE_OF[node], code=code, message=message, **gateway).model_dump(
        mode="json"
    )


def _read[T: BaseModel](value: object, schema: type[T]) -> T:
    try:
        return schema.model_validate(value)
    except ValidationError:
        raise CheckpointStateError(f"invalid {schema.__name__} in graph state") from None


def _read_str(state: InvestigationState, key: Literal["plan_reply", "diagnosis_reply"]) -> str:
    value = state.get(key)
    if not isinstance(value, str):
        raise CheckpointStateError(f"{key} missing from graph state")
    return value


def _read_list(state: InvestigationState, key: Literal["plan", "evidence"]) -> list[Any]:
    value = state.get(key)
    if not isinstance(value, list):
        raise CheckpointStateError(f"{key} missing from graph state")
    return value


def parse_model_output[T: BaseModel](content: str, schema: type[T]) -> T:
    """Strictly parse a model reply; raises ``StageFailedError``."""
    text = content.strip()
    fenced = _FENCE.fullmatch(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            raise StageFailedError(
                "malformed_json", "The model did not return valid JSON."
            ) from None
        raise StageFailedError(
            "schema_violation", "The model output does not match the required schema."
        ) from None


def permitted_plan(
    plan: Plan, allowed_paths: frozenset[str], max_tool_calls: int
) -> list[PlannedTool]:
    """Check every step against the catalog and limits; raises ``StageFailedError``."""
    requests: list[ToolRequest] = []
    for step in plan.steps:
        try:
            request = parse_tool_request(step.tool, step.arguments, allowed_paths)
        except ToolRequestError as exc:
            raise StageFailedError(exc.code, exc.message) from None
        if request in requests:
            raise StageFailedError(
                "duplicate_tool_request", "The plan requests the same diagnostic twice."
            )
        requests.append(request)
    if len(requests) > max_tool_calls:
        raise StageFailedError(
            "tool_call_limit_exceeded",
            f"The plan requests {len(requests)} diagnostics; the limit is {max_tool_calls}.",
        )
    return [
        PlannedTool(tool=request.tool, arguments=request.arguments, reason=step.reason)
        for request, step in zip(requests, plan.steps, strict=True)
    ]


def grounded_diagnosis(diagnosis: Diagnosis, evidence: Sequence[Evidence]) -> Diagnosis:
    """Check every citation against the evidence; raises ``StageFailedError``."""
    collected = {item.id for item in evidence}
    usable = {item.id for item in evidence if item.status is DiagnosticStatus.OK}
    for finding in diagnosis.findings:
        if len(set(finding.evidence)) != len(finding.evidence):
            raise StageFailedError(
                "duplicate_evidence_reference", "A finding cites evidence twice."
            )
        for reference in finding.evidence:
            if reference not in collected:
                raise StageFailedError(
                    "unknown_evidence_reference",
                    "The diagnosis cites evidence that was not collected.",
                )
            if reference not in usable:
                raise StageFailedError(
                    "unusable_evidence_reference",
                    "The diagnosis cites evidence that contains no data.",
                )
    return diagnosis


def _llm_failure(node: Node, outcome: LlmFailure) -> InvestigationState:
    update: InvestigationState = {
        "failure": _failure(
            node,
            outcome.code,
            outcome.message,
            gateway_status=outcome.gateway_status,
            gateway_error_code=outcome.gateway_error_code,
        )
    }
    if outcome.reconciliation_required:
        update["reconciliation_required"] = True
    return update


async def plan(state: InvestigationState, runtime: Runtime[StageContext]) -> InvestigationState:
    ctx = runtime.context
    await ctx.enter("plan")
    messages = planning_messages(await ctx.incident(), ctx.allowed_paths, ctx.max_tool_calls)
    outcome = await ctx.llm.call(OperationType.PLAN, ctx.models.planner, messages)
    if isinstance(outcome, LlmFailure):
        return _llm_failure("plan", outcome)
    return {"plan_reply": outcome.content}


async def validate_plan(
    state: InvestigationState, runtime: Runtime[StageContext]
) -> InvestigationState:
    ctx = runtime.context
    await ctx.enter("validate_plan")
    try:
        parsed = parse_model_output(_read_str(state, "plan_reply"), Plan)
        steps = permitted_plan(parsed, ctx.allowed_paths, ctx.max_tool_calls)
    except StageFailedError as exc:
        return {"failure": _failure("validate_plan", exc.code, exc.message)}
    return {"plan": [step.model_dump(mode="json") for step in steps]}


async def collect_diagnostics(
    state: InvestigationState, runtime: Runtime[StageContext]
) -> InvestigationState:
    ctx = runtime.context
    await ctx.enter("collect_diagnostics")
    evidence: list[Evidence] = []
    for index, raw in enumerate(_read_list(state, "plan"), start=1):
        step = _read(raw, PlannedTool)
        # Re-checked against the allowlist: the plan came from a checkpoint.
        try:
            request = parse_tool_request(step.tool, step.arguments, ctx.allowed_paths)
        except ToolRequestError:
            raise CheckpointStateError("graph state holds an unpermitted tool request") from None
        executed = await ctx.diagnostics.run(request)
        evidence.append(
            Evidence(
                id=f"E{index}",
                tool=request.tool,
                arguments=request.arguments,
                status=executed.result.status,
                data=dict(executed.result.data),
                reason=executed.result.reason,
                duration_ms=executed.duration_ms,
            )
        )
    update: InvestigationState = {"evidence": [item.model_dump(mode="json") for item in evidence]}
    if not any(item.status is DiagnosticStatus.OK for item in evidence):
        # A diagnosis could cite nothing, so the model call would be wasted.
        update["failure"] = _failure(
            "collect_diagnostics", "no_usable_evidence", "No diagnostic produced usable evidence."
        )
    return update


async def diagnose(state: InvestigationState, runtime: Runtime[StageContext]) -> InvestigationState:
    ctx = runtime.context
    await ctx.enter("diagnose")
    evidence = [_read(raw, Evidence) for raw in _read_list(state, "evidence")]
    messages = diagnosis_messages(await ctx.incident(), evidence)
    outcome = await ctx.llm.call(OperationType.DIAGNOSE, ctx.models.diagnosis, messages)
    if isinstance(outcome, LlmFailure):
        return _llm_failure("diagnose", outcome)
    return {"diagnosis_reply": outcome.content}


async def validate_diagnosis(
    state: InvestigationState, runtime: Runtime[StageContext]
) -> InvestigationState:
    await runtime.context.enter("validate_diagnosis")
    evidence = [_read(raw, Evidence) for raw in _read_list(state, "evidence")]
    try:
        parsed = parse_model_output(_read_str(state, "diagnosis_reply"), Diagnosis)
        diagnosis = grounded_diagnosis(parsed, evidence)
    except StageFailedError as exc:
        return {"failure": _failure("validate_diagnosis", exc.code, exc.message)}
    return {"diagnosis": diagnosis.model_dump(mode="json")}


async def build_result(
    state: InvestigationState, ctx: StageContext, failure: Failure | None = None
) -> InvestigationResult:
    """The investigation result for ``state``; ``failure`` overrides the state's."""
    if failure is None and "failure" in state:
        failure = _read(state["failure"], Failure)
    usage = await ctx.llm.usage()
    evidence = [_read(raw, Evidence) for raw in state.get("evidence", [])]
    status: ResultStatus = "completed"
    if failure is not None:
        status = "reconciliation_required" if state.get("reconciliation_required") else "failed"
    return InvestigationResult(
        investigation_id=ctx.investigation_id,
        status=status,
        prompt_version=PROMPT_VERSION,
        plan=[_read(raw, PlannedTool) for raw in state["plan"]] if "plan" in state else None,
        evidence=evidence,
        diagnosis=(
            _read(state["diagnosis"], Diagnosis)
            if failure is None and "diagnosis" in state
            else None
        ),
        failure=failure,
        usage=Usage(
            llm_calls=usage.llm_calls,
            tool_calls=len(evidence),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_ms=round(await ctx.elapsed_ms(), 2),
        ),
    )


async def finalize(state: InvestigationState, runtime: Runtime[StageContext]) -> InvestigationState:
    ctx = runtime.context
    await ctx.enter("finalize")
    if "failure" not in state and "diagnosis" not in state:
        raise CheckpointStateError("finalize reached without a diagnosis or a failure")
    await ctx.finish(await build_result(state, ctx))
    return {}


def _then(next_node: Node) -> Callable[[InvestigationState], Node]:
    def route(state: InvestigationState) -> Node:
        return "finalize" if "failure" in state else next_node

    return route


def build_graph() -> StateGraph[
    InvestigationState, StageContext, InvestigationState, InvestigationState
]:
    graph = StateGraph(InvestigationState, context_schema=StageContext)
    graph.add_node("plan", plan)
    graph.add_node("validate_plan", validate_plan)
    graph.add_node("collect_diagnostics", collect_diagnostics)
    graph.add_node("diagnose", diagnose)
    graph.add_node("validate_diagnosis", validate_diagnosis)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "plan")
    sequence: tuple[tuple[Node, Node], ...] = (
        ("plan", "validate_plan"),
        ("validate_plan", "collect_diagnostics"),
        ("collect_diagnostics", "diagnose"),
        ("diagnose", "validate_diagnosis"),
        ("validate_diagnosis", "finalize"),
    )
    for node, following in sequence:
        graph.add_conditional_edges(node, _then(following), [following, "finalize"])
    graph.add_edge("finalize", END)
    return graph


type CompiledGraph = CompiledStateGraph[
    InvestigationState, StageContext, InvestigationState, InvestigationState
]
