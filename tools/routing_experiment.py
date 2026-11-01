"""Offline comparison of routing policies on recorded evaluation results.

Replays a holdout dataset as a workload. For each case it asks the gateway's
router which model each policy would choose, using evidence from development
runs only, then takes that model's recorded holdout result for the same case
(score, latency, token usage). No provider is called: this is a replay of
recorded calls, not live routing.

Policies compared, all built from one quality_constrained alias:

* fixed: always the alias's primary model (the baseline);
* lowest_cost: the cheapest capable candidate, without quality rules;
* quality_constrained: the alias's configured policy.

The comparison refuses inputs that would make it unfair: holdout runs as
evidence, a holdout that fails ``check_partitions`` against the evidence dataset,
workload runs from another dataset file, or workload runs sent with a different
output allowance or streaming mode. Every policy is priced from recorded token
usage at the configuration's prices at one decision time. Failed calls and
refused requests stay in every denominator, and unknown costs are reported
separately with their upper bound.

Several results files for one target are repetitions; each repetition is
compared separately, and the report shows how much outcomes varied.

Example:

    uv run python tools/routing_experiment.py \\
        --config evaluations/experiments/ticket-routing.toml --alias classify \\
        --evidence-dataset evaluations/datasets/support-ticket-category-v1.jsonl \\
        --evidence results/dev/*.json \\
        --dataset evaluations/datasets/support-ticket-category-v1-holdout.jsonl \\
        --results results/holdout/*.json --allow-simulated
"""

import argparse
import dataclasses
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from gateway.admission.tokens import estimate_input_tokens
from gateway.core.config import GatewayConfig, read_gateway_config
from gateway.cost.pricing import PriceCatalog
from gateway.errors import ConfigurationError, GatewayError
from gateway.evaluation.dataset import (
    Dataset,
    DatasetError,
    Partition,
    check_partitions,
    load_dataset,
)
from gateway.evaluation.plan import OfflineTransport
from gateway.evaluation.runner import TIMEOUT, CaseResult, EvaluationRun, mean_completion_tokens
from gateway.evaluation.stats import (
    paired_difference_interval,
    percentile,
    reported_percentile,
    wilson_interval,
)
from gateway.providers.base import Provider, ProviderRequest
from gateway.providers.factory import build_provider
from gateway.routing.config import RoutingPolicy
from gateway.routing.evidence import Evidence, EvidenceSnapshot
from gateway.routing.router import DecisionReason, RequestProfile, Router, required_capabilities
from gateway.schemas.completions import ChatCompletionRequest

type Target = tuple[str, str]

POLICIES = ("fixed", "lowest_cost", "quality_constrained")
# Each extra repetition multiplies the replayed workload.
MAX_REPETITIONS = 10


class ExperimentError(Exception):
    pass


@dataclass(frozen=True)
class Choice:
    """One policy's decision for one case."""

    target: Target | None
    refused_code: str | None = None
    # The quality-constrained policy found no qualified candidate and used the
    # primary, whose quality was not verified.
    unverified: bool = False


@dataclass
class PolicyRun:
    """One policy's outcomes on one repetition of the workload."""

    policy: str
    requests: int = 0
    refused: Counter[str] = field(default_factory=Counter)
    unverified: int = 0
    passed: int = 0
    invalid: int = 0
    errors: int = 0
    timeouts: int = 0
    costs: list[int] = field(default_factory=list)
    unresolved: int = 0
    unresolved_max_micro_usd: int = 0
    latencies: list[float] = field(default_factory=list)
    routed_to: Counter[str] = field(default_factory=Counter)
    # case ID -> passed; a refused request did not pass.
    outcomes: dict[str, bool] = field(default_factory=dict)

    @property
    def served(self) -> int:
        return self.requests - sum(self.refused.values())

    @property
    def cost(self) -> int:
        return sum(self.costs)


def load_runs(
    paths: Sequence[Path], dataset: Dataset, allow_simulated: bool
) -> dict[Target, list[EvaluationRun]]:
    """Results files per target, oldest first, all produced from ``dataset``."""
    runs: dict[Target, list[EvaluationRun]] = {}
    for path in paths:
        run = EvaluationRun.from_json(json.loads(path.read_text()))
        identity = run.identity
        if identity.dataset_sha256 != dataset.sha256:
            raise ExperimentError(f"{path} was produced from a different dataset file")
        if identity.simulated and not allow_simulated:
            raise ExperimentError(f"{path} is simulated; pass --allow-simulated to use it")
        if not run.complete:
            raise ExperimentError(f"{path} is incomplete ({identity.stop_reason})")
        runs.setdefault((identity.provider, identity.model), []).append(run)
    for target_runs in runs.values():
        target_runs.sort(key=lambda run: run.identity.started_at)
    return runs


def evidence_runs(runs: dict[Target, list[EvaluationRun]]) -> dict[Target, EvaluationRun]:
    evidence = {}
    for (provider, model), target_runs in runs.items():
        if any(run.identity.partition is Partition.HOLDOUT for run in target_runs):
            raise ExperimentError(f"{provider}/{model}: holdout runs cannot be routing evidence")
        # The gateway uses the latest run as evidence; so does the replay.
        evidence[(provider, model)] = target_runs[-1]
    return evidence


def evidence_snapshot(runs: dict[Target, EvaluationRun], dataset: Dataset) -> EvidenceSnapshot:
    return EvidenceSnapshot.of(
        {
            (provider, model, dataset.name, dataset.version): Evidence(
                run_id=run.identity.run_id,
                completed_at=run.identity.completed_at,
                case_count=run.summary.case_count,
                passed_count=run.summary.passed_count,
                latency_samples=run.summary.latency_samples,
                latency_p95_ms=run.summary.latency_p95_ms,
                expected_output_tokens=mean_completion_tokens(run.summary),
                max_output_tokens=run.identity.max_output_tokens,
            )
            for (provider, model), run in runs.items()
        }
    )


def check_workload(
    config: GatewayConfig, alias: str, workload: dict[Target, list[EvaluationRun]]
) -> int:
    """Return the number of repetitions, or raise if the runs are not comparable."""
    model = config.models[alias]
    targets = model.route_targets()
    missing = [f"{p}/{m}" for p, m in targets if (p, m) not in workload]
    if missing:
        raise ExperimentError(f"no workload results for {', '.join(missing)}")
    repetitions = {len(workload[target]) for target in targets}
    if len(repetitions) != 1:
        raise ExperimentError("every target needs the same number of workload runs")
    [count] = repetitions
    if count > MAX_REPETITIONS:
        raise ExperimentError(f"at most {MAX_REPETITIONS} workload runs per target")
    settings = {
        (run.identity.max_output_tokens, run.identity.stream)
        for target in targets
        for run in workload[target]
    }
    if len(settings) != 1:
        raise ExperimentError("workload runs used different max_tokens or streaming settings")
    [(max_tokens, _)] = settings
    if max_tokens != model.output_allowance:
        raise ExperimentError(
            f"workload runs used max_tokens={max_tokens}, but alias {alias!r} sends"
            f" {model.output_allowance}"
        )
    return count


def decide(
    config: GatewayConfig, alias: str, dataset: Dataset, snapshot: EvidenceSnapshot, now: datetime
) -> tuple[dict[str, list[Choice]], dict[str, list[float]]]:
    """Each policy's choice per case, and the router's decision time per case in µs."""
    model = config.models[alias]
    quality = model.routing
    targets = model.route_targets()
    allowance = model.output_allowance
    if allowance is None:
        raise ExperimentError(f"alias {alias!r} has no output bound")
    providers: dict[str, Provider] = {
        name: build_provider(name, settings, SecretStr("offline"), OfflineTransport())
        for name, settings in config.providers.items()
    }
    # The same candidates without quality rules (quality_rules() ignores the
    # remaining fields for this policy).
    lowest = quality.model_copy(update={"policy": RoutingPolicy.LOWEST_COST})
    routers = {
        policy: Router(
            config.model_copy(
                update={
                    "models": {
                        **config.models,
                        alias: model.model_copy(update={"routing": routing}),
                    }
                }
            ),
            lambda _: True,
            lambda: snapshot,
            clock=lambda: now,
        )
        for policy, routing in (("lowest_cost", lowest), ("quality_constrained", quality))
    }
    choices: dict[str, list[Choice]] = {policy: [] for policy in POLICIES}
    timings: dict[str, list[float]] = {policy: [] for policy in routers}
    for case in dataset.cases:
        request = ChatCompletionRequest(model=alias, messages=list(case.messages))
        profile = RequestProfile(
            required_capabilities(request), estimate_input_tokens(case.messages), allowance
        )
        base = ProviderRequest(
            model="",
            messages=tuple(case.messages),
            max_tokens=allowance,
            temperature=None,
            top_p=None,
            stop=(),
            include_usage=False,
        )
        adapter_errors: dict[Target, GatewayError] = {}
        for provider, upstream in targets:
            try:
                providers[provider].validate(dataclasses.replace(base, model=upstream))
            except GatewayError as exc:
                adapter_errors[(provider, upstream)] = exc
        choices["fixed"].append(Choice(targets[0]))
        for policy, router in routers.items():
            started = time.perf_counter_ns()
            try:
                decision = router.decide(alias, profile, adapter_errors)
            except GatewayError as exc:
                choice = Choice(None, refused_code=exc.code)
            else:
                choice = Choice(
                    decision.selected.target,
                    unverified=decision.selection.reason is DecisionReason.NO_QUALIFIED_CANDIDATE,
                )
            timings[policy].append((time.perf_counter_ns() - started) / 1000)
            choices[policy].append(choice)
    return choices, timings


def replay(
    policy: str,
    choices: list[Choice],
    dataset: Dataset,
    results: dict[Target, dict[str, CaseResult]],
    catalog: PriceCatalog,
    now: datetime,
    allowance: int,
) -> PolicyRun:
    run = PolicyRun(policy)
    for case, choice in zip(dataset.cases, choices, strict=True):
        run.requests += 1
        if choice.target is None:
            run.refused[choice.refused_code or "refused"] += 1
            run.outcomes[case.id] = False
            continue
        run.unverified += choice.unverified
        run.routed_to[f"{choice.target[0]}/{choice.target[1]}"] += 1
        result = results[choice.target][case.id]
        price = catalog.select(*choice.target, now)
        if result.prompt_tokens is not None and result.completion_tokens is not None:
            run.costs.append(price.charge_micro_usd(result.prompt_tokens, result.completion_tokens))
        elif result.cost_micro_usd == 0:
            # The provider certainly did not run the call.
            run.costs.append(0)
        else:
            run.unresolved += 1
            run.unresolved_max_micro_usd += price.charge_micro_usd(
                estimate_input_tokens(case.messages), allowance
            )
        run.passed += result.passed
        run.invalid += result.invalid_response
        run.errors += result.error_code is not None
        run.timeouts += result.error_code == TIMEOUT
        if result.latency_ms is not None:
            run.latencies.append(result.latency_ms)
        run.outcomes[case.id] = result.passed
    return run


def compare(
    config: GatewayConfig,
    alias: str,
    dataset: Dataset,
    workload: dict[Target, list[EvaluationRun]],
    evidence_dataset: Dataset,
    evidence: dict[Target, EvaluationRun],
    now: datetime,
) -> dict[str, Any]:
    model = config.models[alias]
    quality = model.routing
    if quality.policy is not RoutingPolicy.QUALITY_CONSTRAINED or quality.min_success_rate is None:
        raise ExperimentError(f"alias {alias!r} must use the quality_constrained policy")
    in_sample = dataset.partition is Partition.DEVELOPMENT
    if not in_sample:
        check_partitions(evidence_dataset, dataset)
    elif dataset.sha256 != evidence_dataset.sha256:
        raise ExperimentError("a development workload must be the evidence dataset itself")
    repetitions = check_workload(config, alias, workload)
    allowance = model.output_allowance
    if allowance is None:
        raise ExperimentError(f"alias {alias!r} has no output bound")
    catalog = PriceCatalog(config.cost.pricing)
    snapshot = evidence_snapshot(evidence, evidence_dataset)
    choices, timings = decide(config, alias, dataset, snapshot, now)
    targets = model.route_targets()
    runs: dict[str, list[PolicyRun]] = {policy: [] for policy in POLICIES}
    for repetition in range(repetitions):
        results = {
            target: {result.case_id: result for result in workload[target][repetition].results}
            for target in targets
        }
        for policy in POLICIES:
            runs[policy].append(
                replay(policy, choices[policy], dataset, results, catalog, now, allowance)
            )

    minimum = quality.min_success_rate
    policies = {policy: _policy_summary(runs[policy], minimum) for policy in POLICIES}
    for policy in ("lowest_cost", "quality_constrained"):
        policies[policy]["quality_vs_fixed"] = _quality_difference(runs["fixed"], runs[policy])
        policies[policy]["savings_vs_fixed"] = _savings(runs["fixed"], runs[policy])
    all_runs = [run for target_runs in workload.values() for run in target_runs]
    all_runs += list(evidence.values())
    simulated = {run.identity.simulated for run in all_runs}
    report: dict[str, Any] = {
        "alias": alias,
        "basis": "replay of recorded evaluation calls",
        "source": "simulated" if simulated == {True} else "mixed" if len(simulated) > 1 else "live",
        "in_sample": in_sample,
        "workload": _dataset_json(dataset) | {"repetitions": repetitions},
        "evidence_dataset": _dataset_json(evidence_dataset),
        "decision_time": now.isoformat(),
        "evidence_version": snapshot.version,
        "quality_rules": {
            "min_success_rate": quality.min_success_rate,
            "min_samples": quality.min_samples,
            "max_evidence_age_days": quality.max_evidence_age_days,
            "max_p95_latency_ms": quality.max_p95_latency_ms,
            "on_insufficient_evidence": quality.on_insufficient_evidence.value,
        },
        "evidence": {
            f"{p}/{m}": {
                "run_id": str(run.identity.run_id),
                "passed": run.summary.passed_count,
                "cases": run.summary.case_count,
                "success_rate_95ci": [run.summary.success_rate_low, run.summary.success_rate_high],
            }
            for (p, m), run in sorted(evidence.items())
        },
        "workload_runs": {
            f"{p}/{m}": [str(run.identity.run_id) for run in workload[(p, m)]] for p, m in targets
        },
        "prices": {
            f"{p}/{m}": price.version
            for p, m in targets
            if (price := catalog.find(p, m, now)) is not None
        },
        "recorded_pricing_versions": sorted(
            {run.identity.pricing_version or "none" for run in all_runs}
        ),
        "routing_decision_us": {
            policy: {"p50": percentile(samples, 0.5), "p95": percentile(samples, 0.95)}
            for policy, samples in timings.items()
        },
        "variability": _variability(workload, targets),
        "policies": policies,
    }
    report["limitations"] = _limitations(report, len(dataset.cases))
    return report


def _dataset_json(dataset: Dataset) -> dict[str, Any]:
    return {
        "name": dataset.name,
        "version": dataset.version,
        "partition": dataset.partition.value,
        "sha256": dataset.sha256,
        "cases": len(dataset.cases),
    }


def _verdict(passed: int, requests: int, minimum: float) -> str:
    low, _ = wilson_interval(passed, requests)
    if low >= minimum:
        return "passes"
    if passed / requests < minimum:
        return "fails"
    return "inconclusive"


def _policy_summary(runs: list[PolicyRun], minimum: float) -> dict[str, Any]:
    requests = sum(run.requests for run in runs)
    passed = sum(run.passed for run in runs)
    errors = sum(run.errors for run in runs)
    costs = [cost for run in runs for cost in run.costs]
    latencies = [latency for run in runs for latency in run.latencies]
    verdicts = {_verdict(run.passed, run.requests, minimum) for run in runs}
    refused: Counter[str] = sum((run.refused for run in runs), Counter())
    routed: Counter[str] = sum((run.routed_to for run in runs), Counter())
    if verdicts == {"passes"}:
        threshold = "passes"
    elif "fails" in verdicts:
        threshold = "fails"
    else:
        threshold = "inconclusive"
    summary: dict[str, Any] = {
        "requests": requests,
        "served": sum(run.served for run in runs),
        "refused": dict(refused),
        "unverified_fallbacks": sum(run.unverified for run in runs),
        "passed": passed,
        # Refused requests and failed calls count as not passed.
        "success_rate": passed / requests,
        # Repetitions reuse the same cases, so they are not independent samples.
        "success_rate_95ci": list(wilson_interval(passed, requests)) if len(runs) == 1 else None,
        "quality_threshold": threshold,
        "invalid_responses": sum(run.invalid for run in runs),
        "provider_errors": errors,
        "timeouts": sum(run.timeouts for run in runs),
        "error_rate": errors / requests,
        "observed_cost_micro_usd": sum(costs),
        "unresolved_cases": sum(run.unresolved for run in runs),
        "unresolved_max_micro_usd": sum(run.unresolved_max_micro_usd for run in runs),
        "cost_per_request_micro_usd": (
            {f"p{q}": percentile([float(c) for c in costs], q / 100) for q in (50, 95, 100)}
            if costs
            else None
        ),
        "latency_ms": {
            f"p{round(q * 100)}": reported_percentile(latencies, q) for q in (0.5, 0.95, 0.99)
        },
        "routed_to": dict(sorted(routed.items())),
    }
    if len(runs) > 1:
        summary["repetitions"] = [
            {
                "passed": run.passed,
                "success_rate": run.passed / run.requests,
                "success_rate_95ci": list(wilson_interval(run.passed, run.requests)),
                "observed_cost_micro_usd": run.cost,
                "unresolved_cases": run.unresolved,
            }
            for run in runs
        ]
    return summary


def _quality_difference(baseline: list[PolicyRun], other: list[PolicyRun]) -> dict[str, Any]:
    """Success-rate difference (policy minus fixed) on the same cases."""
    per_repetition: list[dict[str, Any]] = []
    for base, run in zip(baseline, other, strict=True):
        only_policy = sum(run.outcomes[c] and not base.outcomes[c] for c in run.outcomes)
        only_fixed = sum(base.outcomes[c] and not run.outcomes[c] for c in run.outcomes)
        low, high = paired_difference_interval(only_policy, only_fixed, run.requests)
        per_repetition.append(
            {
                "difference": (only_policy - only_fixed) / run.requests,
                "difference_95ci": [low, high],
                "cases_only_policy_passed": only_policy,
                "cases_only_fixed_passed": only_fixed,
            }
        )
    if len(per_repetition) == 1:
        return per_repetition[0]
    return {
        "difference": sum(r["difference"] for r in per_repetition) / len(per_repetition),
        "difference_95ci": None,
        "repetitions": per_repetition,
    }


def _savings(baseline: list[PolicyRun], other: list[PolicyRun]) -> dict[str, Any]:
    base = sum(run.cost for run in baseline)
    cost = sum(run.cost for run in other)
    base_unknown = sum(run.unresolved_max_micro_usd for run in baseline)
    unknown = sum(run.unresolved_max_micro_usd for run in other)
    savings: dict[str, Any] = {
        "baseline_observed_micro_usd": base,
        "observed_micro_usd": cost,
        "absolute_micro_usd": base - cost,
        "percent": None,
        "percent_range": None,
        "reason": None,
    }
    if any(run.refused for run in [*baseline, *other]):
        savings["reason"] = "a policy refused requests, so the policies served different work"
    elif base == 0:
        savings["reason"] = "the baseline's observed cost is zero"
    elif base_unknown or unknown:
        # Each true total lies between its observed cost and observed plus unknown.
        savings["reason"] = "some costs are unknown; the range covers every possible value"
        savings["percent_range"] = [
            round((base - cost - unknown) / base * 100, 2),
            round((1 - cost / (base + base_unknown)) * 100, 2),
        ]
    else:
        savings["percent"] = round((base - cost) / base * 100, 2)
    return savings


def _variability(
    workload: dict[Target, list[EvaluationRun]], targets: list[Target]
) -> dict[str, Any] | None:
    """How often each target's case outcomes agreed across repetitions."""
    if len(workload[targets[0]]) < 2:
        return None
    variability = {}
    for provider, model in targets:
        runs = workload[(provider, model)]
        outcomes = [{r.case_id: r.passed for r in run.results} for run in runs]
        cases = outcomes[0].keys()
        agreeing = sum(len({outcome[case] for outcome in outcomes}) == 1 for case in cases)
        rates = [run.summary.success_rate for run in runs]
        variability[f"{provider}/{model}"] = {
            "case_agreement": agreeing / len(cases),
            "success_rate_range": [min(rates), max(rates)],
        }
    return variability


def _limitations(report: dict[str, Any], cases: int) -> list[str]:
    limitations = [
        "Replay of recorded evaluation calls: routing decisions were recomputed, but no"
        " request went through the gateway, so retries, fallbacks, circuit breakers, and"
        " budget refusals were not exercised.",
        "Latency is each evaluation call's wall-clock time from the evaluation client, not"
        " gateway latency under load.",
        "Costs are recorded token usage at the configured prices, not provider invoices.",
    ]
    if report["source"] != "live":
        limitations.insert(0, "SIMULATED inputs: the results test the tooling, not any model.")
    if report["in_sample"]:
        limitations.append(
            "In-sample: the workload is the evidence dataset, so quality is optimistic."
        )
    if report["variability"] is None:
        limitations.append(
            "One run per model: response variability was not measured, and a rerun of a"
            " nondeterministic model can score differently."
        )
    limitations.append(
        f"{cases} workload cases per run: compare the paired difference intervals before"
        " treating two policies as equivalent in quality."
    )
    if any(p["unresolved_cases"] for p in report["policies"].values()):
        limitations.append("Some costs are unknown; their upper bounds are reported separately.")
    if set(report["recorded_pricing_versions"]) - set(report["prices"].values()):
        limitations.append(
            "Some runs were recorded at other price versions; all costs were recomputed at"
            " the configuration's current prices."
        )
    return limitations


def markdown(report: dict[str, Any]) -> str:
    workload = report["workload"]
    lines = [
        f"Alias `{report['alias']}`: {report['basis']}, {report['source']} results,"
        f" {workload['cases']} {workload['partition']} cases x {workload['repetitions']} run(s).",
        "",
        "| Policy | Requests | Refused | Success (95% CI) | Quality vs fixed (95% CI) |"
        " Threshold | Observed cost | Unknown cost (max) | Savings vs fixed | Latency p50 / p95 |"
        " Routed to |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, summary in report["policies"].items():
        interval = summary["success_rate_95ci"]
        success = f"{summary['success_rate']:.3f}" + (
            f" ({interval[0]:.3f}-{interval[1]:.3f})" if interval else ""
        )
        lines.append(
            f"| {name} | {summary['requests']} | {sum(summary['refused'].values())} |"
            f" {success} | {_difference_cell(summary.get('quality_vs_fixed'))} |"
            f" {summary['quality_threshold']} | {summary['observed_cost_micro_usd']} |"
            f" {summary['unresolved_cases']} ({summary['unresolved_max_micro_usd']}) |"
            f" {_savings_cell(summary.get('savings_vs_fixed'))} |"
            f" {summary['latency_ms']['p50']} / {summary['latency_ms']['p95']} |"
            f" {', '.join(f'{k}: {v}' for k, v in summary['routed_to'].items())} |"
        )
    lines += ["", "Limitations:", *(f"- {item}" for item in report["limitations"])]
    return "\n".join(lines)


def _difference_cell(difference: dict[str, Any] | None) -> str:
    if difference is None:
        return "baseline"
    interval = difference["difference_95ci"]
    text = f"{difference['difference']:+.3f}"
    return text + (f" ({interval[0]:+.3f} to {interval[1]:+.3f})" if interval else "")


def _savings_cell(savings: dict[str, Any] | None) -> str:
    if savings is None:
        return "baseline"
    if savings["percent"] is not None:
        return f"{savings['percent']}%"
    if savings["percent_range"] is not None:
        low, high = savings["percent_range"]
        return f"{low}% to {high}%"
    return str(savings["reason"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--dataset", type=Path, required=True, help="workload (holdout) dataset")
    parser.add_argument("--results", type=Path, nargs="+", required=True, help="workload runs")
    parser.add_argument("--evidence-dataset", type=Path, help="dataset of the evidence runs")
    parser.add_argument(
        "--evidence",
        type=Path,
        nargs="+",
        help="development runs used as routing evidence (default: the workload, in-sample)",
    )
    parser.add_argument("--allow-simulated", action="store_true")
    parser.add_argument("--output", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)
    try:
        if (args.evidence is None) != (args.evidence_dataset is None):
            raise ExperimentError("--evidence and --evidence-dataset go together")
        dataset = load_dataset(args.dataset)
        workload = load_runs(args.results, dataset, args.allow_simulated)
        evidence_dataset = load_dataset(args.evidence_dataset) if args.evidence else dataset
        evidence = evidence_runs(
            load_runs(args.evidence, evidence_dataset, args.allow_simulated)
            if args.evidence
            else workload
        )
        if not evidence:
            raise ExperimentError("no evidence runs")
        # Evaluate freshness as of the newest evidence, so a replay gives the same answer later.
        now = max(run.identity.completed_at for run in evidence.values())
        report = compare(
            read_gateway_config(args.config),
            args.alias,
            dataset,
            workload,
            evidence_dataset,
            evidence,
            now,
        )
    except (ExperimentError, ConfigurationError, DatasetError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
