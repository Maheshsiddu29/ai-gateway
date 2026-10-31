import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.core.config import GatewayConfig
from gateway.errors import (
    GatewayError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from gateway.routing.config import AliasRouting, Capability, ModelCapabilities
from gateway.routing.estimate import CostEstimate
from gateway.routing.evidence import Evidence, EvidenceSnapshot
from gateway.routing.router import (
    CandidateFacts,
    DecisionReason,
    Exclusion,
    RequestProfile,
    Router,
    Selection,
    in_rollout,
    required_capabilities,
    select,
)
from gateway.schemas.completions import ChatCompletionRequest

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
PROFILE = RequestProfile(frozenset(), estimated_input_tokens=100, output_allowance=50)
LOWEST = AliasRouting.model_validate(
    {"policy": "lowest_cost", "candidates": [{"provider": "x", "model": "y"}]}
)


def quality(**overrides: Any) -> AliasRouting:
    fields: dict[str, Any] = {
        "policy": "quality_constrained",
        "candidates": [{"provider": "x", "model": "y"}],
        "dataset": "tickets",
        "dataset_version": "1",
        "min_success_rate": 0.8,
        "min_samples": 40,
        "max_evidence_age_days": 30,
    }
    fields.update(overrides)
    return AliasRouting.model_validate(fields)


def evidence(
    passed: int = 50,
    cases: int = 50,
    *,
    age: timedelta = timedelta(days=1),
    p95: float | None = 200.0,
    latency_samples: int | None = None,
) -> Evidence:
    return Evidence(
        run_id=uuid.uuid4(),
        completed_at=NOW - age,
        case_count=cases,
        passed_count=passed,
        latency_samples=cases if latency_samples is None else latency_samples,
        latency_p95_ms=p95,
        expected_output_tokens=10,
    )


def candidate(
    model: str,
    cost: int | None,
    *,
    provider: str = "p",
    available: bool = True,
    adapter_error: GatewayError | None = None,
    evidence: Evidence | None = None,
    capabilities: frozenset[Capability] = frozenset(Capability),
    window: int = 10_000,
    max_output: int = 1000,
    routable: bool = True,
) -> CandidateFacts:
    return CandidateFacts(
        provider=provider,
        model=model,
        capabilities=ModelCapabilities(
            provider=provider,
            model=model,
            context_window_tokens=window,
            max_output_tokens=max_output,
            capabilities=capabilities,
            routable=routable,
        ),
        adapter_error=adapter_error,
        available=available,
        estimate=None if cost is None else CostEstimate("v1", 100, 50, cost, None, None),
        evidence=evidence,
    )


def models(chain: Sequence[CandidateFacts]) -> list[str]:
    return [c.model for c in chain]


class TestLowestCost:
    def test_selects_cheapest_and_orders_fallbacks_by_cost(self) -> None:
        selection = select(
            LOWEST,
            PROFILE,
            [candidate("big", 900), candidate("small", 100), candidate("mid", 400)],
            NOW,
        )

        assert selection.reason is DecisionReason.LOWEST_COST
        assert models(selection.chain) == ["small", "mid", "big"]
        assert selection.eligible_count == 3

    def test_equal_costs_follow_configuration_order(self) -> None:
        facts = [candidate("primary", 100), candidate("second", 100), candidate("third", 50)]

        assert models(select(LOWEST, PROFILE, facts, NOW).chain) == ["third", "primary", "second"]

    def test_attempt_chain_is_bounded(self) -> None:
        facts = [candidate(f"m{i}", 10 * (8 - i)) for i in range(8)]

        selection = select(LOWEST, PROFILE, facts, NOW)

        assert models(selection.chain) == ["m7", "m6", "m5", "m4"]
        assert selection.eligible_count == 8

    @pytest.mark.parametrize(
        ("cheap", "exclusion"),
        [
            (candidate("cheap", 1, capabilities=frozenset()), Exclusion.MISSING_CAPABILITY),
            (candidate("cheap", 1, max_output=49), Exclusion.OUTPUT_LIMIT),
            (candidate("cheap", 1, window=149), Exclusion.CONTEXT_WINDOW),
            (
                candidate("cheap", 1, adapter_error=UnsupportedCapabilityError("no")),
                Exclusion.ADAPTER_REJECTED,
            ),
            (candidate("cheap", 1, routable=False), Exclusion.NOT_ROUTABLE),
            (candidate("cheap", None), Exclusion.MISSING_PRICE),
            (candidate("cheap", 1, available=False), Exclusion.CIRCUIT_OPEN),
        ],
    )
    def test_ineligible_candidates_are_excluded_with_a_reason(
        self, cheap: CandidateFacts, exclusion: Exclusion
    ) -> None:
        profile = RequestProfile(frozenset({Capability.STREAMING}), 100, 50)

        selection = select(LOWEST, profile, [candidate("primary", 500), cheap], NOW)

        assert models(selection.chain) == ["primary"]
        assert selection.exclusions == ((("p", "cheap"), exclusion),)

    def test_context_window_counts_input_and_output(self) -> None:
        fits = candidate("fits", 1, window=150)

        assert (
            models(select(LOWEST, PROFILE, [candidate("primary", 9), fits], NOW).chain)[0] == "fits"
        )

    def test_no_capable_candidate_is_an_unsupported_capability(self) -> None:
        profile = RequestProfile(frozenset({Capability.TEMPERATURE}), 100, 50)
        facts = [candidate("a", 1, capabilities=frozenset()), candidate("b", 1, max_output=1)]

        with pytest.raises(UnsupportedCapabilityError) as error:
            select(LOWEST, profile, facts, NOW)
        assert error.value.code == "unsupported_by_route"

    def test_primary_adapter_error_is_reported_when_nothing_can_run(self) -> None:
        rejection = UnsupportedCapabilityError("temperature unsupported", code="x_rejected")
        facts = [
            candidate("a", 1, adapter_error=rejection),
            candidate("b", 1, capabilities=frozenset()),
        ]
        profile = RequestProfile(frozenset({Capability.TOP_P}), 100, 50)

        with pytest.raises(UnsupportedCapabilityError) as error:
            select(LOWEST, profile, [facts[0], candidate("b", 1, max_output=1)], NOW)
        assert error.value is rejection

    def test_all_capable_candidates_with_open_circuits(self) -> None:
        facts = [candidate("a", 1, available=False), candidate("b", 2, available=False)]

        with pytest.raises(ProviderUnavailableError) as error:
            select(LOWEST, PROFILE, facts, NOW)
        assert error.value.code == "provider_circuit_open"
        assert error.value.may_have_processed is False

    def test_unpriced_candidates_never_count_as_free(self) -> None:
        with pytest.raises(ProviderUnavailableError) as error:
            select(LOWEST, PROFILE, [candidate("a", None), candidate("b", None)], NOW)
        assert error.value.code == "no_eligible_route"


class TestQualityConstrained:
    def test_selects_the_cheapest_qualified_candidate(self) -> None:
        facts = [
            candidate("large", 900, evidence=evidence(50, 50)),
            candidate("small", 100, evidence=evidence(30, 50)),
            candidate("medium", 400, evidence=evidence(49, 50)),
        ]

        selection = select(quality(), PROFILE, facts, NOW)

        assert selection.reason is DecisionReason.QUALIFIED_LOWEST_COST
        assert models(selection.chain) == ["medium", "large"]
        assert selection.exclusions == ((("p", "small"), Exclusion.BELOW_QUALITY),)

    @pytest.mark.parametrize(
        ("item", "exclusion"),
        [
            (None, Exclusion.NO_EVIDENCE),
            (evidence(age=timedelta(days=31)), Exclusion.STALE_EVIDENCE),
            (evidence(age=-timedelta(seconds=1)), Exclusion.INVALID_EVIDENCE_TIME),
            (evidence(39, 39), Exclusion.INSUFFICIENT_SAMPLES),
            (evidence(40, 50), Exclusion.BELOW_QUALITY),
        ],
    )
    def test_evidence_rules(self, item: Evidence | None, exclusion: Exclusion) -> None:
        facts = [
            candidate("primary", 900, evidence=evidence()),
            candidate("cheap", 1, evidence=item),
        ]

        selection = select(quality(), PROFILE, facts, NOW)

        assert models(selection.chain) == ["primary"]
        assert dict(selection.exclusions)[("p", "cheap")] is exclusion

    def test_a_perfect_score_on_a_small_sample_does_not_qualify(self) -> None:
        # 10/10 has a Wilson lower bound of about 0.72.
        rules = quality(min_samples=10, min_success_rate=0.9)
        facts = [
            candidate("primary", 900, evidence=evidence()),
            candidate("cheap", 1, evidence=evidence(10, 10)),
        ]

        assert dict(select(rules, PROFILE, facts, NOW).exclusions)[("p", "cheap")] is (
            Exclusion.BELOW_QUALITY
        )

    def test_evidence_exactly_at_the_age_limit_is_fresh(self) -> None:
        facts = [candidate("only", 1, evidence=evidence(age=timedelta(days=30)))]

        assert models(select(quality(), PROFILE, facts, NOW).chain) == ["only"]

    @pytest.mark.parametrize(
        ("item", "exclusion"),
        [
            (evidence(p95=None), Exclusion.NO_LATENCY_EVIDENCE),
            (evidence(latency_samples=39), Exclusion.NO_LATENCY_EVIDENCE),
            (evidence(p95=1000.5), Exclusion.LATENCY_ABOVE_LIMIT),
            (evidence(p95=1000.0), None),
        ],
    )
    def test_latency_constraint(self, item: Evidence, exclusion: Exclusion | None) -> None:
        facts = [
            candidate("primary", 900, evidence=evidence(p95=10)),
            candidate("cheap", 1, evidence=item),
        ]

        selection = select(quality(max_p95_latency_ms=1000), PROFILE, facts, NOW)

        assert dict(selection.exclusions).get(("p", "cheap")) is exclusion

    def test_latency_is_ignored_without_a_latency_limit(self) -> None:
        facts = [candidate("only", 1, evidence=evidence(p95=None))]

        assert models(select(quality(), PROFILE, facts, NOW).chain) == ["only"]

    def test_insufficient_evidence_uses_the_primary(self) -> None:
        facts = [candidate("primary", 900), candidate("cheap", 1)]

        selection = select(quality(), PROFILE, facts, NOW)

        assert selection.reason is DecisionReason.NO_QUALIFIED_CANDIDATE
        assert models(selection.chain) == ["primary"]
        assert selection.eligible_count == 0
        assert set(dict(selection.exclusions).values()) == {Exclusion.NO_EVIDENCE}

    def test_insufficient_evidence_can_reject(self) -> None:
        facts = [candidate("primary", 900), candidate("cheap", 1)]

        with pytest.raises(ProviderUnavailableError) as error:
            select(quality(on_insufficient_evidence="reject"), PROFILE, facts, NOW)
        assert error.value.code == "no_eligible_route"

    def test_primary_fallback_still_requires_capabilities(self) -> None:
        profile = RequestProfile(frozenset({Capability.STREAMING}), 100, 50)
        facts = [candidate("primary", 900, capabilities=frozenset()), candidate("cheap", 1)]

        # The cheap model could run the request but is not qualified.
        with pytest.raises(ProviderUnavailableError) as error:
            select(quality(), profile, facts, NOW)
        assert error.value.code == "no_eligible_route"


@st.composite
def candidate_sets(draw: st.DrawFn) -> list[CandidateFacts]:
    count = draw(st.integers(min_value=1, max_value=8))
    return [
        candidate(
            f"m{index}",
            draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5))),
            available=draw(st.booleans()),
            evidence=draw(
                st.one_of(
                    st.none(),
                    st.builds(
                        evidence,
                        st.integers(min_value=0, max_value=60),
                        st.just(60),
                        age=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=60)),
                    ),
                )
            ),
        )
        for index in range(count)
    ]


@given(candidate_sets(), st.sampled_from([LOWEST, quality()]))
def test_selection_is_deterministic_and_cost_ordered(
    facts: list[CandidateFacts], routing: AliasRouting
) -> None:
    def attempt() -> Selection | tuple[type[GatewayError], str]:
        try:
            return select(routing, PROFILE, facts, NOW)
        except GatewayError as exc:
            return (type(exc), exc.code)

    first = attempt()
    assert first == attempt()
    if not isinstance(first, tuple) and first.reason is not DecisionReason.NO_QUALIFIED_CANDIDATE:
        costs = [c.estimate.maximum_micro_usd for c in first.chain if c.estimate is not None]
        assert costs == sorted(costs)
        excluded = {target for target, _ in first.exclusions}
        cheapest_eligible = min(
            (c.estimate.maximum_micro_usd, i)
            for i, c in enumerate(facts)
            if c.target not in excluded and c.estimate is not None
        )
        assert first.chain[0] is facts[cheapest_eligible[1]]


def test_rollout_assignment_is_stable_and_proportional() -> None:
    principals = [f"key-{index:012x}" for index in range(2000)]

    assigned = [in_rollout("classify", principal, 25) for principal in principals]

    assert assigned == [in_rollout("classify", principal, 25) for principal in principals]
    assert 0.2 < sum(assigned) / len(assigned) < 0.3
    assert not any(in_rollout("classify", principal, 0) for principal in principals)
    assert all(in_rollout("classify", principal, 100) for principal in principals)


def test_required_capabilities_follow_request_fields() -> None:
    def request(**fields: Any) -> ChatCompletionRequest:
        return ChatCompletionRequest.model_validate(
            {"model": "a", "messages": [{"role": "user", "content": "x"}], **fields}
        )

    assert required_capabilities(request()) == frozenset()
    assert required_capabilities(
        request(
            stream=True, stream_options={"include_usage": True}, temperature=0, top_p=1, stop="x"
        )
    ) == frozenset(Capability)


def routed_config(**routing: Any) -> GatewayConfig:
    def price(model: str, version: str, effective: str, rate: str) -> dict[str, str]:
        return {
            "provider": "p",
            "model": model,
            "version": version,
            "effective_at": effective,
            "input_per_million_usd": rate,
            "output_per_million_usd": rate,
        }

    def entry(model: str) -> dict[str, Any]:
        return {
            "provider": "p",
            "model": model,
            "context_window_tokens": 10_000,
            "max_output_tokens": 100,
        }

    return GatewayConfig.model_validate(
        {
            "providers": {"p": {"type": "openai_compatible", "base_url": "https://p.test/v1"}},
            "models": {
                "a": {
                    "provider": "p",
                    "model": "large",
                    "max_output_tokens": 50,
                    "routing": {"candidates": [{"provider": "p", "model": "small"}], **routing},
                }
            },
            "cost": {
                "enabled": True,
                "pricing": [
                    price("large", "l1", "2020-01-01T00:00:00Z", "2"),
                    price("small", "s1", "2020-01-01T00:00:00Z", "1"),
                    price("small", "s2", "2026-11-01T00:00:00Z", "3"),
                ],
            },
            "routing": {"enabled": True, "models": [entry("large"), entry("small")]},
        }
    )


class TestRouter:
    def test_prices_all_candidates_at_the_decision_time(self) -> None:
        config = routed_config(policy="lowest_cost")
        now = NOW
        router = Router(config, lambda _: True, EvidenceSnapshot, clock=lambda: now)

        before = router.decide("a", PROFILE, {})
        now = datetime(2026, 11, 1, tzinfo=UTC)
        after = router.decide("a", PROFILE, {})

        assert (before.selected.model, before.selected.estimate) == (
            "small",
            CostEstimate("s1", 100, 50, 150, None, None),
        )
        # The newer, higher small-model price makes the large model cheaper.
        assert after.selected.model == "large"
        assert [c.estimate.pricing_version for c in after.selection.chain if c.estimate] == [
            "l1",
            "s2",
        ]
        assert before.policy_version == after.policy_version
        assert before.evidence_version is None

    def test_reads_one_evidence_snapshot_per_decision(self) -> None:
        config = routed_config(
            policy="quality_constrained",
            dataset="tickets",
            dataset_version="1",
            min_success_rate=0.5,
            min_samples=10,
            max_evidence_age_days=7,
        )
        good = EvidenceSnapshot.of({("p", "small", "tickets", "1"): evidence()})
        reads: list[EvidenceSnapshot] = []

        def current() -> EvidenceSnapshot:
            reads.append(good if not reads else EvidenceSnapshot())
            return reads[-1]

        decision = Router(config, lambda _: True, current, clock=lambda: NOW).decide(
            "a", PROFILE, {}
        )

        assert len(reads) == 1
        assert decision.selected.model == "small"
        assert decision.selected.evidence is not None
        assert decision.evidence_version == good.version != "empty"

    def test_breaker_availability_and_adapter_results_are_inputs(self) -> None:
        router = Router(
            routed_config(policy="lowest_cost"), lambda provider: provider != "p", EvidenceSnapshot
        )

        with pytest.raises(ProviderUnavailableError, match="temporarily unavailable"):
            router.decide("a", PROFILE, {})

        router = Router(routed_config(policy="lowest_cost"), lambda _: True, EvidenceSnapshot)
        decision = router.decide("a", PROFILE, {("p", "small"): UnsupportedCapabilityError("no")})
        assert decision.selected.model == "large"
        assert decision.selection.exclusions == ((("p", "small"), Exclusion.ADAPTER_REJECTED),)

    def test_policy_version_changes_with_prices_and_rules(self) -> None:
        base = Router(routed_config(policy="lowest_cost"), lambda _: True, EvidenceSnapshot)
        other = Router(
            routed_config(policy="lowest_cost", rollout_percent=50),
            lambda _: True,
            EvidenceSnapshot,
        )

        assert (
            base.decide("a", PROFILE, {}).policy_version
            != other.decide("a", PROFILE, {}).policy_version
        )
