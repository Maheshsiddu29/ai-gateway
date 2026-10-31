"""Routing configuration: the model capability registry and per-alias policies.

Registry entries describe what an upstream model can do through this gateway. They
are operator-declared facts; the gateway cannot discover them, so an entry
advertises a capability only if the operator lists it and the provider's adapter
implements it (checked at startup in ``gateway.core.config``).
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gateway.schemas.completions import MAX_OUTPUT_TOKENS

# Bounds the per-request routing work and the size of each decision log event.
MAX_ROUTE_CANDIDATES = 8
# A routed request tries at most this many candidates, the same bound as a primary
# with the maximum number of configured fallbacks.
MAX_ROUTE_ATTEMPT_CHAIN = 4

DatasetName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
DatasetVersion = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")]


class Capability(StrEnum):
    """Request features a model must support to serve a request."""

    STREAMING = "streaming"
    # Token usage reported at the end of a stream (stream_options.include_usage).
    STREAM_USAGE = "stream_usage"
    TEMPERATURE = "temperature"
    TOP_P = "top_p"
    STOP = "stop"


class RouteTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    model: str = Field(min_length=1, max_length=256)


class ModelCapabilities(BaseModel):
    """What one upstream model supports. Every limit is required: a model whose
    limits are unknown cannot be checked and so cannot be routed to dynamically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    model: str = Field(min_length=1, max_length=256)
    context_window_tokens: int = Field(ge=1, le=100_000_000)
    max_output_tokens: int = Field(ge=1, le=MAX_OUTPUT_TOKENS)
    capabilities: frozenset[Capability] = frozenset()
    # False takes the model out of dynamic routing without deleting its entry.
    routable: bool = True

    @property
    def target(self) -> tuple[str, str]:
        return self.provider, self.model


class RoutingPolicy(StrEnum):
    FIXED = "fixed"
    LOWEST_COST = "lowest_cost"
    QUALITY_CONSTRAINED = "quality_constrained"


class InsufficientEvidence(StrEnum):
    """What a quality-constrained alias does when no candidate qualifies."""

    # Send the request to the alias's configured primary model.
    PRIMARY = "primary"
    # Refuse the request.
    REJECT = "reject"


@dataclass(frozen=True)
class QualityRules:
    dataset: str
    dataset_version: str
    min_success_rate: float
    min_samples: int
    max_evidence_age: timedelta
    max_p95_latency_ms: int | None


_QUALITY_FIELDS = (
    "dataset",
    "dataset_version",
    "min_success_rate",
    "min_samples",
    "max_evidence_age_days",
)


class AliasRouting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: RoutingPolicy = RoutingPolicy.FIXED
    # Considered together with the alias's primary model, which is always a candidate.
    candidates: list[RouteTarget] = Field(default_factory=list, max_length=MAX_ROUTE_CANDIDATES - 1)
    # Share of gateway keys, assigned by a stable hash, that use the policy; the
    # others use the primary model.
    rollout_percent: int = Field(default=100, ge=0, le=100)
    # Quality evidence: the evaluation dataset that represents this alias's task.
    dataset: DatasetName | None = None
    dataset_version: DatasetVersion | None = None
    # Compared with the lower bound of the 95% Wilson interval of the measured
    # success rate, not with the point estimate.
    min_success_rate: float | None = Field(default=None, ge=0, le=1)
    min_samples: int | None = Field(default=None, ge=1, le=1_000_000)
    max_evidence_age_days: int | None = Field(default=None, ge=1, le=366)
    # Optional; compared with the p95 latency measured by the same evaluation run.
    max_p95_latency_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    on_insufficient_evidence: InsufficientEvidence = InsufficientEvidence.PRIMARY

    @model_validator(mode="after")
    def _check_policy_fields(self) -> "AliasRouting":
        quality_set = [name for name in _QUALITY_FIELDS if getattr(self, name) is not None]
        if self.max_p95_latency_ms is not None:
            quality_set.append("max_p95_latency_ms")
        if self.policy is RoutingPolicy.FIXED and self.candidates:
            raise ValueError("a fixed routing policy takes no candidates")
        if self.policy is not RoutingPolicy.QUALITY_CONSTRAINED and quality_set:
            raise ValueError(
                f"{', '.join(quality_set)} only apply to the quality_constrained policy"
            )
        if self.policy is RoutingPolicy.QUALITY_CONSTRAINED:
            missing = [name for name in _QUALITY_FIELDS if getattr(self, name) is None]
            if missing:
                raise ValueError(f"quality_constrained routing requires {', '.join(missing)}")
        if self.policy is not RoutingPolicy.FIXED and not self.candidates:
            raise ValueError(f"the {self.policy.value} policy requires candidates")
        targets = [(target.provider, target.model) for target in self.candidates]
        if len(set(targets)) != len(targets):
            raise ValueError("routing candidates must not repeat")
        return self

    def quality_rules(self) -> QualityRules | None:
        if self.policy is not RoutingPolicy.QUALITY_CONSTRAINED:
            return None
        if (
            self.dataset is None
            or self.dataset_version is None
            or self.min_success_rate is None
            or self.min_samples is None
            or self.max_evidence_age_days is None
        ):
            raise ValueError("quality_constrained routing is missing evidence rules")
        return QualityRules(
            dataset=self.dataset,
            dataset_version=self.dataset_version,
            min_success_rate=self.min_success_rate,
            min_samples=self.min_samples,
            max_evidence_age=timedelta(days=self.max_evidence_age_days),
            max_p95_latency_ms=self.max_p95_latency_ms,
        )


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # When false, every alias uses its primary model, whatever its routing policy.
    enabled: bool = False
    models: list[ModelCapabilities] = Field(default_factory=list, max_length=1000)
    # How often evaluation evidence is reloaded from PostgreSQL.
    evidence_refresh_seconds: float = Field(default=60.0, ge=1, le=3600)

    @model_validator(mode="after")
    def _check_unique(self) -> "RoutingConfig":
        targets = [entry.target for entry in self.models]
        if len(set(targets)) != len(targets):
            raise ValueError("routing.models must not repeat a provider and model")
        return self

    def registry(self) -> dict[tuple[str, str], ModelCapabilities]:
        return {entry.target: entry for entry in self.models}
