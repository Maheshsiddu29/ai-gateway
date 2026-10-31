"""Offline comparison of routing policies on recorded evaluation results.

Replays an evaluation dataset as a workload. For each case it asks the gateway's
router which model each policy would choose, then takes that model's recorded
result for the same case (score, latency, token usage) from a results file
written by ``python -m gateway.evaluation run``. No provider is called.

Policies compared, all built from one quality_constrained alias:

* fixed: always the alias's primary model (the baseline);
* lowest_cost: the cheapest capable candidate, without quality rules;
* quality_constrained: the alias's configured policy.

Costs are recomputed from recorded token usage at the configuration's current
prices, so every policy is priced identically. A case without recorded usage is
costed at its maximum estimate and marked estimated; savings are only reported
when both policies have observed costs for every case and no refused requests.

Example:

    uv run python tools/routing_experiment.py \\
        --config evaluations/experiments/ticket-routing.toml --alias classify \\
        --dataset evaluations/datasets/support-ticket-category-v1.jsonl \\
        --results results/*.json --allow-simulated
"""

import argparse
import dataclasses
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from gateway.admission.tokens import estimate_input_tokens
from gateway.core.config import GatewayConfig, read_gateway_config
from gateway.cost.pricing import PriceCatalog
from gateway.errors import ConfigurationError, GatewayError
from gateway.evaluation.dataset import Dataset, DatasetError, load_dataset
from gateway.evaluation.runner import CaseResult, EvaluationRun, mean_completion_tokens
from gateway.evaluation.stats import percentile, wilson_interval
from gateway.providers.base import Provider, ProviderRequest
from gateway.providers.factory import build_provider
from gateway.routing.config import RoutingPolicy
from gateway.routing.evidence import Evidence, EvidenceSnapshot
from gateway.routing.router import RequestProfile, Router, required_capabilities
from gateway.schemas.completions import ChatCompletionRequest

type Target = tuple[str, str]


class ExperimentError(Exception):
    pass


class _OfflineTransport(httpx.AsyncBaseTransport):
    """Adapters are built only for their request validation, which does no I/O."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise ExperimentError("the experiment must not call providers")


@dataclass
class PolicyReport:
    policy: str
    requests: int = 0
    refused: Counter[str] = field(default_factory=Counter)
    passed: int = 0
    invalid: int = 0
    errors: int = 0
    costs: list[int] = field(default_factory=list)
    estimated_costs: int = 0
    latencies: list[float] = field(default_factory=list)
    routed_to: Counter[str] = field(default_factory=Counter)

    def to_json(self, min_success_rate: float) -> dict[str, Any]:
        served = len(self.costs)
        low, high = wilson_interval(self.passed, served) if served else (0.0, 1.0)
        success_rate = self.passed / served if served else None
        return {
            "policy": self.policy,
            "requests": self.requests,
            "served": served,
            "refused": dict(self.refused),
            "passed": self.passed,
            "invalid_responses": self.invalid,
            "provider_errors": self.errors,
            "success_rate": success_rate,
            "success_rate_95ci": [low, high],
            "below_min_success_rate": success_rate is not None and success_rate < min_success_rate,
            "total_cost_micro_usd": sum(self.costs),
            "estimated_cost_cases": self.estimated_costs,
            "cost_per_request_micro_usd": _distribution([float(c) for c in self.costs]),
            "latency_ms": _distribution(self.latencies, (0.5, 0.95, 0.99)),
            "routed_to": dict(sorted(self.routed_to.items())),
        }


def _distribution(
    samples: Sequence[float], quantiles: Sequence[float] = (0.5, 0.95, 1.0)
) -> dict[str, float] | None:
    if not samples:
        return None
    return {f"p{round(q * 100)}": percentile(samples, q) for q in quantiles}


def load_runs(
    paths: Sequence[Path], dataset: Dataset, allow_simulated: bool
) -> dict[Target, EvaluationRun]:
    runs: dict[Target, EvaluationRun] = {}
    for path in paths:
        run = EvaluationRun.from_json(json.loads(path.read_text()))
        identity = run.identity
        if identity.dataset_sha256 != dataset.sha256:
            raise ExperimentError(f"{path} was produced from a different dataset file")
        if identity.simulated and not allow_simulated:
            raise ExperimentError(f"{path} is simulated; pass --allow-simulated to use it")
        target = (identity.provider, identity.model)
        if target in runs:
            raise ExperimentError(f"more than one results file for {target[0]}/{target[1]}")
        runs[target] = run
    return runs


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
            )
            for (provider, model), run in runs.items()
        }
    )


def compare(
    config: GatewayConfig,
    alias: str,
    dataset: Dataset,
    workload: dict[Target, EvaluationRun],
    evidence: dict[Target, EvaluationRun],
    now: datetime,
) -> dict[str, Any]:
    model = config.models[alias]
    quality = model.routing
    if quality.policy is not RoutingPolicy.QUALITY_CONSTRAINED or quality.min_success_rate is None:
        raise ExperimentError(f"alias {alias!r} must use the quality_constrained policy")
    targets = model.route_targets()
    missing = [f"{p}/{m}" for p, m in targets if (p, m) not in workload]
    if missing:
        raise ExperimentError(f"no results for {', '.join(missing)}")
    allowance = model.output_allowance
    if allowance is None:
        raise ExperimentError(f"alias {alias!r} has no output bound")
    results = {
        target: {result.case_id: result for result in run.results}
        for target, run in workload.items()
    }
    providers: dict[str, Provider] = {
        name: build_provider(name, settings, SecretStr("offline"), _OfflineTransport())
        for name, settings in config.providers.items()
    }
    catalog = PriceCatalog(config.cost.pricing)
    snapshot = evidence_snapshot(evidence, dataset)
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
    reports = {name: PolicyReport(name) for name in ("fixed", "lowest_cost", "quality_constrained")}

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
        choices: dict[str, Target | GatewayError] = {"fixed": targets[0]}
        for name, router in routers.items():
            try:
                choices[name] = router.decide(alias, profile, adapter_errors).selected.target
            except GatewayError as exc:
                choices[name] = exc
        for name, choice in choices.items():
            report = reports[name]
            report.requests += 1
            if isinstance(choice, GatewayError):
                report.refused[choice.code] += 1
                continue
            _record(report, choice, results[choice][case.id], catalog, now, profile)

    fixed = reports["fixed"]
    summaries = {name: report.to_json(quality.min_success_rate) for name, report in reports.items()}
    for name in ("lowest_cost", "quality_constrained"):
        summaries[name]["savings_vs_fixed"] = _savings(fixed, reports[name])
    return {
        "alias": alias,
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "sha256": dataset.sha256,
            "cases": len(dataset.cases),
        },
        "simulated": any(
            run.identity.simulated for run in [*workload.values(), *evidence.values()]
        ),
        "in_sample": {target: run.identity.run_id for target, run in workload.items()}
        == {target: run.identity.run_id for target, run in evidence.items()},
        "decision_time": now.isoformat(),
        "evidence_version": snapshot.version,
        "quality_rules": {
            "min_success_rate": quality.min_success_rate,
            "min_samples": quality.min_samples,
            "max_evidence_age_days": quality.max_evidence_age_days,
            "max_p95_latency_ms": quality.max_p95_latency_ms,
        },
        "evidence": {
            f"{p}/{m}": {
                "passed": run.summary.passed_count,
                "cases": run.summary.case_count,
                "success_rate_95ci": [run.summary.success_rate_low, run.summary.success_rate_high],
            }
            for (p, m), run in sorted(evidence.items())
        },
        "prices": {
            f"{p}/{m}": price.version
            for p, m in targets
            if (price := catalog.find(p, m, now)) is not None
        },
        "policies": summaries,
    }


def _record(
    report: PolicyReport,
    target: Target,
    result: CaseResult,
    catalog: PriceCatalog,
    now: datetime,
    profile: RequestProfile,
) -> None:
    report.routed_to[f"{target[0]}/{target[1]}"] += 1
    price = catalog.select(*target, now)
    if result.prompt_tokens is not None and result.completion_tokens is not None:
        report.costs.append(price.charge_micro_usd(result.prompt_tokens, result.completion_tokens))
    else:
        report.costs.append(
            price.charge_micro_usd(profile.estimated_input_tokens, profile.output_allowance)
        )
        report.estimated_costs += 1
    report.passed += result.passed
    report.invalid += result.invalid_response
    report.errors += result.error_code is not None
    if result.latency_ms is not None:
        report.latencies.append(result.latency_ms)


def _savings(baseline: PolicyReport, candidate: PolicyReport) -> dict[str, Any]:
    if baseline.refused or candidate.refused:
        return {"percent": None, "reason": "a policy refused requests, so the workloads differ"}
    if baseline.estimated_costs or candidate.estimated_costs:
        return {"percent": None, "reason": "some costs are estimates, not recorded usage"}
    base, optimized = sum(baseline.costs), sum(candidate.costs)
    if base == 0:
        return {"percent": None, "reason": "the baseline cost is zero"}
    return {"percent": round((base - optimized) / base * 100, 2), "reason": None}


def markdown(report: dict[str, Any]) -> str:
    lines = []
    if report["simulated"]:
        lines.append("**SIMULATED RESULTS: they test the tooling, not any real model.**\n")
    if report["in_sample"]:
        lines.append("Evidence and workload are the same runs (in-sample).\n")
    lines.append(
        "| Policy | Served | Refused | Success (95% CI) | Total cost (micro-USD) |"
        " Cost p50 / p95 / max | Savings vs fixed | Routed to |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for summary in report["policies"].values():
        rate = summary["success_rate"]
        low, high = summary["success_rate_95ci"]
        costs = summary["cost_per_request_micro_usd"] or {}
        savings = summary.get("savings_vs_fixed")
        saving = (
            "baseline"
            if savings is None
            else f"{savings['percent']}%"
            if savings["percent"] is not None
            else savings["reason"]
        )
        lines.append(
            f"| {summary['policy']} | {summary['served']} | {sum(summary['refused'].values())} |"
            f" {'n/a' if rate is None else f'{rate:.3f} ({low:.3f}-{high:.3f})'} |"
            f" {summary['total_cost_micro_usd']} |"
            f" {costs.get('p50')} / {costs.get('p95')} / {costs.get('p100')} | {saving} |"
            f" {', '.join(f'{k}: {v}' for k, v in summary['routed_to'].items())} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument(
        "--evidence",
        type=Path,
        nargs="+",
        help="results files used as routing evidence (default: --results, which is in-sample)",
    )
    parser.add_argument("--allow-simulated", action="store_true")
    parser.add_argument("--output", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.dataset)
        workload = load_runs(args.results, dataset, args.allow_simulated)
        evidence = (
            load_runs(args.evidence, dataset, args.allow_simulated) if args.evidence else workload
        )
        if not evidence:
            raise ExperimentError("no evidence runs")
        # Evaluate freshness as of the newest evidence, so a replay gives the same answer later.
        now = max(run.identity.completed_at for run in evidence.values())
        report = compare(
            read_gateway_config(args.config), args.alias, dataset, workload, evidence, now
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
