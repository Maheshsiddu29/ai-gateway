"""Schemas for model output and for the investigation API.

``Plan`` and ``Diagnosis`` describe what the model must return. Model output is
untrusted: it is parsed strictly, unknown fields are rejected, and every string
and list is bounded. Passing the schema does not make a plan permitted or a
diagnosis grounded; the workflow checks both separately.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent_runtime.diagnostics.tools import DataValue, DiagnosticStatus, ToolName

MAX_INCIDENT_CHARS = 4000
# An absolute ceiling; AGENT_MAX_TOOL_CALLS sets the effective limit, at most this.
MAX_PLAN_STEPS = 16


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PlannedStep(_Strict):
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=4)
    reason: str = Field(min_length=1, max_length=300)


class Plan(_Strict):
    steps: list[PlannedStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)


class Finding(_Strict):
    claim: str = Field(min_length=1, max_length=500)
    # Checked against the collected evidence by the workflow, not by pattern, so an
    # invented reference is reported as such.
    evidence: list[Annotated[str, Field(min_length=1, max_length=16)]] = Field(
        min_length=1, max_length=8
    )


class Diagnosis(_Strict):
    conclusion: Literal["supported", "inconclusive"]
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[Finding] = Field(default_factory=list, max_length=10)
    # Suggestions for a human operator. The runtime never acts on them.
    next_steps: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=5
    )

    @model_validator(mode="after")
    def _supported_needs_findings(self) -> "Diagnosis":
        if self.conclusion == "supported" and not self.findings:
            raise ValueError("a supported conclusion needs at least one finding")
        return self


class InvestigationRequest(_Strict):
    incident: str = Field(min_length=1, max_length=MAX_INCIDENT_CHARS)


class Stage(StrEnum):
    PLANNING = "planning"
    DIAGNOSTICS = "diagnostics"
    DIAGNOSIS = "diagnosis"


class PlannedTool(BaseModel):
    """A plan step after validation: a permitted tool and the model's stated reason."""

    tool: ToolName
    arguments: dict[str, str]
    reason: str


class Evidence(BaseModel):
    id: str
    tool: ToolName
    arguments: dict[str, str]
    status: DiagnosticStatus
    data: dict[str, DataValue]
    reason: str | None
    duration_ms: float


class Failure(BaseModel):
    stage: Stage
    code: str
    message: str
    # From the gateway's response, when the failure was a gateway call.
    gateway_status: int | None = None
    gateway_error_code: str | None = None


class Usage(BaseModel):
    llm_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float


class InvestigationResult(BaseModel):
    investigation_id: str
    status: Literal["completed", "failed"]
    prompt_version: str
    plan: list[PlannedTool] | None
    evidence: list[Evidence]
    diagnosis: Diagnosis | None
    failure: Failure | None
    usage: Usage
