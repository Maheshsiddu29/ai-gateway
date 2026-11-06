"""The bounded, evidence-grounded Linux investigation.

1. Ask the planner model, through the gateway, for diagnostics to run.
2. Parse the plan strictly and check every step against the tool catalog and the
   allowed paths. Reject duplicates and plans over the tool-call limit.
3. Run the permitted diagnostics, sequentially, and number the evidence.
4. Ask the diagnosis model for a conclusion based on that evidence.
5. Parse the diagnosis strictly and check that every cited evidence ID was
   collected successfully.

Any problem ends the investigation with a ``Failure`` naming the stage and a
stable code; nothing falls back to a less validated path. Limits on model calls,
tool calls, output tokens, and total duration apply to every investigation.
"""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ValidationError

from agent_runtime.diagnostics.tools import (
    DiagnosticExecutor,
    DiagnosticStatus,
    ToolRequest,
    ToolRequestError,
    parse_tool_request,
)
from agent_runtime.gateway_client import ChatMessage, GatewayCallError, GatewayCompletion
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

logger = logging.getLogger(__name__)

# One optional Markdown code fence around the whole reply. Nothing else is
# stripped or repaired before strict parsing.
_FENCE = re.compile(r"```(?:json)?[ \t]*\n(?P<body>.*)\n[ \t]*```", re.DOTALL)


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


@dataclass(frozen=True)
class ModelAliases:
    planner: str
    diagnosis: str


class InvestigationFailedError(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


@dataclass
class _Run:
    """State of one investigation. Never shared between investigations."""

    investigation_id: str
    started: float = field(default_factory=time.perf_counter)
    stage: Stage = Stage.PLANNING
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    plan: list[PlannedTool] | None = None
    evidence: list[Evidence] = field(default_factory=list)
    diagnosis: Diagnosis | None = None

    def failed(
        self, code: str, message: str, error: GatewayCallError | None = None
    ) -> InvestigationFailedError:
        return InvestigationFailedError(
            Failure(
                stage=self.stage,
                code=code,
                message=message,
                gateway_status=error.status_code if error is not None else None,
                gateway_error_code=error.gateway_error_code if error is not None else None,
            )
        )

    def result(self, failure: Failure | None) -> InvestigationResult:
        return InvestigationResult(
            investigation_id=self.investigation_id,
            status="failed" if failure is not None else "completed",
            prompt_version=PROMPT_VERSION,
            plan=self.plan,
            evidence=self.evidence,
            diagnosis=self.diagnosis if failure is None else None,
            failure=failure,
            usage=Usage(
                llm_calls=self.llm_calls,
                tool_calls=len(self.evidence),
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                duration_ms=round((time.perf_counter() - self.started) * 1000, 2),
            ),
        )


def new_investigation_id() -> str:
    return f"inv-{uuid.uuid4().hex}"


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
        run = _Run(investigation_id)
        failure: Failure | None = None
        deadline = asyncio.timeout(self._limits.timeout_seconds)
        try:
            async with deadline:
                await self._run(run, incident)
        except InvestigationFailedError as exc:
            failure = exc.failure
        except TimeoutError:
            if not deadline.expired():
                raise
            failure = Failure(
                stage=run.stage,
                code="investigation_timeout",
                message=f"The investigation exceeded {self._limits.timeout_seconds} seconds.",
            )
        result = run.result(failure)
        logger.info(
            "investigation finished",
            extra={
                "investigation_id": investigation_id,
                "principal_id": principal_id,
                "status": result.status,
                "failure_stage": failure.stage.value if failure is not None else None,
                "failure_code": failure.code if failure is not None else None,
                "conclusion": result.diagnosis.conclusion if result.diagnosis else None,
                "llm_calls": result.usage.llm_calls,
                "tool_calls": result.usage.tool_calls,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "duration_ms": result.usage.duration_ms,
            },
        )
        return result

    async def _run(self, run: _Run, incident: str) -> None:
        run.stage = Stage.PLANNING
        content = await self._call(
            run,
            self._models.planner,
            planning_messages(incident, self._allowed_paths, self._limits.max_tool_calls),
        )
        plan = _parse(run, content, Plan)
        requests = self._permitted(run, plan)

        run.stage = Stage.DIAGNOSTICS
        for index, request in enumerate(requests, start=1):
            executed = await self._diagnostics.run(request)
            run.evidence.append(
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
        usable = {item.id for item in run.evidence if item.status is DiagnosticStatus.OK}
        if not usable:
            # A diagnosis could cite nothing, so the model call would be wasted.
            raise run.failed("no_usable_evidence", "No diagnostic produced usable evidence.")

        run.stage = Stage.DIAGNOSIS
        content = await self._call(
            run, self._models.diagnosis, diagnosis_messages(incident, run.evidence)
        )
        diagnosis = _parse(run, content, Diagnosis)
        collected = {item.id for item in run.evidence}
        for finding in diagnosis.findings:
            if len(set(finding.evidence)) != len(finding.evidence):
                raise run.failed("duplicate_evidence_reference", "A finding cites evidence twice.")
            for reference in finding.evidence:
                if reference not in collected:
                    raise run.failed(
                        "unknown_evidence_reference",
                        "The diagnosis cites evidence that was not collected.",
                    )
                if reference not in usable:
                    raise run.failed(
                        "unusable_evidence_reference",
                        "The diagnosis cites evidence that contains no data.",
                    )
        run.diagnosis = diagnosis

    def _permitted(self, run: _Run, plan: Plan) -> list[ToolRequest]:
        requests: list[ToolRequest] = []
        for step in plan.steps:
            try:
                request = parse_tool_request(step.tool, step.arguments, self._allowed_paths)
            except ToolRequestError as exc:
                raise run.failed(exc.code, exc.message) from None
            if request in requests:
                raise run.failed(
                    "duplicate_tool_request", "The plan requests the same diagnostic twice."
                )
            requests.append(request)
        if len(requests) > self._limits.max_tool_calls:
            raise run.failed(
                "tool_call_limit_exceeded",
                f"The plan requests {len(requests)} diagnostics; the limit is"
                f" {self._limits.max_tool_calls}.",
            )
        run.plan = [
            PlannedTool(tool=request.tool, arguments=request.arguments, reason=step.reason)
            for request, step in zip(requests, plan.steps, strict=True)
        ]
        return requests

    async def _call(self, run: _Run, model: str, messages: list[ChatMessage]) -> str:
        if run.llm_calls >= self._limits.max_llm_calls:
            raise run.failed(
                "llm_call_limit_exceeded",
                f"The investigation reached its limit of {self._limits.max_llm_calls} model calls.",
            )
        run.llm_calls += 1
        try:
            completion = await self._gateway.complete(
                model=model,
                messages=messages,
                max_tokens=self._limits.max_output_tokens,
                request_id=f"{run.investigation_id}-llm{run.llm_calls}",
            )
        except GatewayCallError as exc:
            if exc.usage is not None:
                run.prompt_tokens += exc.usage.prompt_tokens
                run.completion_tokens += exc.usage.completion_tokens
            raise run.failed(exc.code.value, exc.message, exc) from None
        if completion.usage is not None:
            run.prompt_tokens += completion.usage.prompt_tokens
            run.completion_tokens += completion.usage.completion_tokens
        return completion.content


def _parse[T: BaseModel](run: _Run, content: str, schema: type[T]) -> T:
    text = content.strip()
    fenced = _FENCE.fullmatch(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            raise run.failed("malformed_json", "The model did not return valid JSON.") from None
        raise run.failed(
            "schema_violation", "The model output does not match the required schema."
        ) from None
