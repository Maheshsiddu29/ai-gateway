"""Routing decisions for aliases with a dynamic policy.

``Router.decide`` gathers facts about each candidate (registry capabilities, the
adapter's validation result, circuit-breaker availability, a cost estimate from
one pricing snapshot, and evaluation evidence from one evidence snapshot) and
passes them to ``select``, a pure function of the policy, the request profile,
the facts, and the decision time.

Candidates are filtered, then ranked by maximum estimated cost, the amount a
budget reservation would hold, with configuration order (primary first) breaking
ties. The result is the ordered list of candidates the executor tries; later
entries are the fallbacks. Identical inputs always give the identical decision.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from gateway.core.config import GatewayConfig
from gateway.cost.pricing import PriceCatalog
from gateway.errors import GatewayError, ProviderUnavailableError, UnsupportedCapabilityError
from gateway.routing.config import (
    MAX_ROUTE_ATTEMPT_CHAIN,
    AliasRouting,
    Capability,
    InsufficientEvidence,
    ModelCapabilities,
    QualityRules,
    RoutingPolicy,
)
from gateway.routing.estimate import CostEstimate, estimate_cost
from gateway.routing.evidence import Evidence, EvidenceSnapshot
from gateway.schemas.completions import ChatCompletionRequest

type Target = tuple[str, str]


def required_capabilities(request: ChatCompletionRequest) -> frozenset[Capability]:
    required: set[Capability] = set()
    if request.stream:
        required.add(Capability.STREAMING)
    if request.include_usage:
        required.add(Capability.STREAM_USAGE)
    if request.temperature is not None:
        required.add(Capability.TEMPERATURE)
    if request.top_p is not None:
        required.add(Capability.TOP_P)
    if request.stop:
        required.add(Capability.STOP)
    return frozenset(required)


def in_rollout(alias: str, principal_id: str, percent: int) -> bool:
    """Stable assignment of a gateway key to a policy's rollout share."""
    if percent >= 100:
        return True
    digest = hashlib.sha256(f"{alias}\0{principal_id}".encode()).digest()
    return int.from_bytes(digest[:8]) % 100 < percent


@dataclass(frozen=True)
class RequestProfile:
    capabilities: frozenset[Capability]
    estimated_input_tokens: int
    output_allowance: int


class Exclusion(StrEnum):
    MISSING_CAPABILITY = "missing_capability"
    OUTPUT_LIMIT = "output_limit"
    CONTEXT_WINDOW = "context_window"
    ADAPTER_REJECTED = "adapter_rejected"
    NOT_ROUTABLE = "not_routable"
    MISSING_PRICE = "missing_price"
    CIRCUIT_OPEN = "circuit_open"
    NO_EVIDENCE = "no_evidence"
    # The evaluation allowed more output than this request does, so truncation the
    # request may suffer was not measured.
    EVIDENCE_OUTPUT_LIMIT = "evidence_output_limit"
    INVALID_EVIDENCE_TIME = "invalid_evidence_time"
    STALE_EVIDENCE = "stale_evidence"
    INSUFFICIENT_SAMPLES = "insufficient_samples"
    BELOW_QUALITY = "below_quality"
    NO_LATENCY_EVIDENCE = "no_latency_evidence"
    LATENCY_ABOVE_LIMIT = "latency_above_limit"


# Exclusions meaning the candidate cannot run this request as given.
_INCAPABLE = frozenset(
    {
        Exclusion.MISSING_CAPABILITY,
        Exclusion.OUTPUT_LIMIT,
        Exclusion.CONTEXT_WINDOW,
        Exclusion.ADAPTER_REJECTED,
    }
)


class DecisionReason(StrEnum):
    LOWEST_COST = "lowest_cost"
    QUALIFIED_LOWEST_COST = "qualified_lowest_cost"
    # Quality-constrained, nothing qualified, and the policy sends such requests
    # to the alias's primary model. The primary's quality was not verified.
    NO_QUALIFIED_CANDIDATE = "no_qualified_candidate"


@dataclass(frozen=True)
class CandidateFacts:
    provider: str
    model: str
    capabilities: ModelCapabilities
    adapter_error: GatewayError | None
    available: bool
    estimate: CostEstimate | None
    evidence: Evidence | None

    @property
    def target(self) -> Target:
        return self.provider, self.model


@dataclass(frozen=True)
class Selection:
    reason: DecisionReason
    # The candidates to try, in order.
    chain: tuple[CandidateFacts, ...]
    eligible_count: int
    exclusions: tuple[tuple[Target, Exclusion], ...]


@dataclass(frozen=True)
class RoutingDecision:
    alias: str
    policy: RoutingPolicy
    selection: Selection
    candidate_count: int
    decided_at: datetime
    # Hash of the alias's routing configuration, the registry entries, and the
    # prices of its targets; with evidence_version, identifies every input.
    policy_version: str
    evidence_version: str | None

    @property
    def selected(self) -> CandidateFacts:
        return self.selection.chain[0]

    @property
    def quality_verified(self) -> bool:
        """The selected model passed the alias's quality rules. False for
        lowest_cost and for a primary used because no candidate qualified."""
        return self.selection.reason is DecisionReason.QUALIFIED_LOWEST_COST


def select(
    routing: AliasRouting,
    profile: RequestProfile,
    facts: Sequence[CandidateFacts],
    now: datetime,
) -> Selection:
    """Apply the policy. ``facts[0]`` is the alias's primary model."""
    rules = routing.quality_rules()
    exclusions: list[tuple[Target, Exclusion]] = []
    eligible: list[tuple[int, int, CandidateFacts]] = []
    for index, candidate in enumerate(facts):
        exclusion = _exclusion(rules, profile, candidate, now)
        if exclusion is not None:
            exclusions.append((candidate.target, exclusion))
        elif candidate.estimate is not None:
            eligible.append((candidate.estimate.maximum_micro_usd, index, candidate))

    if eligible:
        # Cheapest maximum cost first; configuration order breaks ties.
        eligible.sort(key=lambda item: item[:2])
        reason = (
            DecisionReason.LOWEST_COST if rules is None else DecisionReason.QUALIFIED_LOWEST_COST
        )
        chain = tuple(candidate for _, _, candidate in eligible[:MAX_ROUTE_ATTEMPT_CHAIN])
        return Selection(reason, chain, len(eligible), tuple(exclusions))

    by_target = dict(exclusions)
    primary = facts[0]
    if (
        rules is not None
        and routing.on_insufficient_evidence is InsufficientEvidence.PRIMARY
        and by_target[primary.target] not in _INCAPABLE
    ):
        return Selection(DecisionReason.NO_QUALIFIED_CANDIDATE, (primary,), 0, tuple(exclusions))
    raise _no_route_error(facts, by_target)


def _exclusion(
    rules: QualityRules | None,
    profile: RequestProfile,
    candidate: CandidateFacts,
    now: datetime,
) -> Exclusion | None:
    model = candidate.capabilities
    if not profile.capabilities <= model.capabilities:
        return Exclusion.MISSING_CAPABILITY
    if profile.output_allowance > model.max_output_tokens:
        return Exclusion.OUTPUT_LIMIT
    if profile.estimated_input_tokens + profile.output_allowance > model.context_window_tokens:
        return Exclusion.CONTEXT_WINDOW
    if candidate.adapter_error is not None:
        return Exclusion.ADAPTER_REJECTED
    if not model.routable:
        return Exclusion.NOT_ROUTABLE
    if candidate.estimate is None:
        return Exclusion.MISSING_PRICE
    if not candidate.available:
        return Exclusion.CIRCUIT_OPEN
    if rules is not None:
        return quality_exclusion(rules, candidate.evidence, profile.output_allowance, now)
    return None


def quality_exclusion(
    rules: QualityRules, evidence: Evidence | None, output_allowance: int, now: datetime
) -> Exclusion | None:
    """Why ``evidence`` does not qualify a model under ``rules``, or None if it does."""
    if evidence is None:
        return Exclusion.NO_EVIDENCE
    if evidence.max_output_tokens > output_allowance:
        return Exclusion.EVIDENCE_OUTPUT_LIMIT
    if evidence.completed_at > now:
        return Exclusion.INVALID_EVIDENCE_TIME
    if now - evidence.completed_at > rules.max_evidence_age:
        return Exclusion.STALE_EVIDENCE
    if evidence.case_count < rules.min_samples:
        return Exclusion.INSUFFICIENT_SAMPLES
    if evidence.success_rate_low < rules.min_success_rate:
        return Exclusion.BELOW_QUALITY
    if rules.max_p95_latency_ms is not None:
        if evidence.latency_p95_ms is None or evidence.latency_samples < rules.min_samples:
            return Exclusion.NO_LATENCY_EVIDENCE
        if evidence.latency_p95_ms > rules.max_p95_latency_ms:
            return Exclusion.LATENCY_ABOVE_LIMIT
    return None


def _no_route_error(
    facts: Sequence[CandidateFacts], exclusions: Mapping[Target, Exclusion]
) -> GatewayError:
    capable = [c for c in facts if exclusions[c.target] not in _INCAPABLE]
    if not capable:
        primary = facts[0]
        if primary.adapter_error is not None:
            return primary.adapter_error
        return UnsupportedCapabilityError(
            "No model configured for this alias supports the requested parameters.",
            code="unsupported_by_route",
        )
    if all(exclusions[c.target] is Exclusion.CIRCUIT_OPEN for c in capable):
        return ProviderUnavailableError(
            "The provider is temporarily unavailable.",
            code="provider_circuit_open",
            may_have_processed=False,
        )
    return ProviderUnavailableError(
        "No model configured for this alias currently satisfies its routing policy.",
        code="no_eligible_route",
        may_have_processed=False,
    )


class Router:
    def __init__(
        self,
        config: GatewayConfig,
        availability: Callable[[str], bool],
        evidence: Callable[[], EvidenceSnapshot],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._models = config.models
        self._registry = config.routing.registry()
        self._catalog = PriceCatalog(config.cost.pricing)
        self._availability = availability
        self._evidence = evidence
        self._clock = clock
        self._policy_versions = {
            alias: _policy_version(config, alias)
            for alias, model in config.models.items()
            if model.routing.policy is not RoutingPolicy.FIXED
        }

    def targets(self, alias: str) -> list[Target]:
        return self._models[alias].route_targets()

    def decide(
        self,
        alias: str,
        profile: RequestProfile,
        adapter_errors: Mapping[Target, GatewayError],
    ) -> RoutingDecision:
        routing = self._models[alias].routing
        now = self._clock()
        snapshot = self._evidence() if routing.policy is RoutingPolicy.QUALITY_CONSTRAINED else None
        rules = routing.quality_rules()
        facts = [
            self._facts(target, rules, profile, adapter_errors.get(target), snapshot, now)
            for target in self.targets(alias)
        ]
        return RoutingDecision(
            alias=alias,
            policy=routing.policy,
            selection=select(routing, profile, facts, now),
            candidate_count=len(facts),
            decided_at=now,
            policy_version=self._policy_versions[alias],
            evidence_version=snapshot.version if snapshot is not None else None,
        )

    def _facts(
        self,
        target: Target,
        rules: QualityRules | None,
        profile: RequestProfile,
        adapter_error: GatewayError | None,
        snapshot: EvidenceSnapshot | None,
        now: datetime,
    ) -> CandidateFacts:
        provider, model = target
        evidence = None
        if snapshot is not None and rules is not None:
            evidence = snapshot.runs.get((provider, model, rules.dataset, rules.dataset_version))
        return CandidateFacts(
            provider=provider,
            model=model,
            capabilities=self._registry[target],
            adapter_error=adapter_error,
            available=self._availability(provider),
            estimate=estimate_cost(
                self._catalog,
                provider,
                model,
                now,
                estimated_input_tokens=profile.estimated_input_tokens,
                maximum_output_tokens=profile.output_allowance,
                expected_output_tokens=evidence.expected_output_tokens if evidence else None,
            ),
            evidence=evidence,
        )


def _policy_version(config: GatewayConfig, alias: str) -> str:
    model = config.models[alias]
    registry = config.routing.registry()
    targets = model.route_targets()
    document = {
        "routing": model.routing.model_dump(mode="json"),
        "targets": [registry[target].model_dump(mode="json") for target in targets],
        "prices": [
            price.model_dump(mode="json")
            for price in config.cost.pricing
            if (price.provider, price.model) in targets
        ],
        "output": [model.default_max_tokens, model.max_output_tokens],
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16]
