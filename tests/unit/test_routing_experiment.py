import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from gateway.core.config import GatewayConfig, read_gateway_config
from gateway.evaluation.dataset import Dataset, DatasetError, load_dataset
from gateway.evaluation.runner import EvaluationRun, run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.stats import paired_difference_interval
from gateway.providers.base import Completion
from gateway.routing.config import InsufficientEvidence
from routing_experiment import ExperimentError, PolicyRun, _savings, compare, evidence_runs, main
from tests.fakes import FakeProvider

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "evaluations" / "experiments" / "ticket-routing.toml"
DATASETS = ROOT / "evaluations" / "datasets"
TICKETS = DATASETS / "support-ticket-category-v1.jsonl"
TICKETS_HOLDOUT = DATASETS / "support-ticket-category-v1-holdout.jsonl"
INVOICES = DATASETS / "invoice-fields-v1.jsonl"
ACCURACY = {"small": 0.80, "medium": 0.93, "large": 0.98}

type Runs = dict[tuple[str, str], list[EvaluationRun]]


async def simulated_runs(
    path: Path, accuracy: dict[str, float] = ACCURACY, max_tokens: int = 8
) -> Runs:
    dataset = load_dataset(path)
    provider = SimulatedProvider("sim", dataset, accuracy)
    return {
        ("sim", model): [
            await run_evaluation(
                provider, model, dataset, max_output_tokens=max_tokens, price=None, simulated=True
            )
        ]
        for model in accuracy
    }


def latest(runs: Runs) -> dict[tuple[str, str], EvaluationRun]:
    return {target: target_runs[-1] for target, target_runs in runs.items()}


def evidence_time(evidence: Runs) -> datetime:
    return max(run.identity.completed_at for runs in evidence.values() for run in runs)


def holdout_report(
    workload: Runs, evidence: Runs, config: GatewayConfig | None = None
) -> dict[str, Any]:
    return compare(
        config or read_gateway_config(CONFIG),
        "classify",
        load_dataset(TICKETS_HOLDOUT),
        workload,
        load_dataset(TICKETS),
        latest(evidence),
        evidence_time(evidence),
    )


async def test_compares_policies_on_the_same_holdout_workload() -> None:
    evidence = await simulated_runs(TICKETS)
    workload = await simulated_runs(TICKETS_HOLDOUT)

    report = holdout_report(workload, evidence)

    policies = report["policies"]
    assert report["source"] == "simulated"
    assert report["in_sample"] is False
    assert report["workload"]["partition"] == "holdout"
    assert {name: p["requests"] for name, p in policies.items()} == dict.fromkeys(policies, 60)
    assert policies["fixed"]["routed_to"] == {"sim/large": 60}
    assert policies["lowest_cost"]["routed_to"] == {"sim/small": 60}
    assert policies["quality_constrained"]["routed_to"] == {"sim/medium": 60}
    assert policies["quality_constrained"]["unverified_fallbacks"] == 0
    fixed_cost = policies["fixed"]["observed_cost_micro_usd"]
    savings = policies["quality_constrained"]["savings_vs_fixed"]
    assert savings["absolute_micro_usd"] == fixed_cost - savings["observed_micro_usd"]
    assert savings["percent"] == round(savings["absolute_micro_usd"] / fixed_cost * 100, 2)
    difference = policies["lowest_cost"]["quality_vs_fixed"]
    low, high = difference["difference_95ci"]
    assert low <= difference["difference"] <= high
    assert set(report["routing_decision_us"]) == {"lowest_cost", "quality_constrained"}
    assert report["variability"] is None
    limitations = " ".join(report["limitations"])
    assert "SIMULATED" in limitations
    assert "One run per model" in limitations
    assert "Replay" in limitations


async def test_report_is_reproducible() -> None:
    evidence = await simulated_runs(TICKETS)
    workload = await simulated_runs(TICKETS_HOLDOUT)

    first = holdout_report(workload, evidence)
    second = holdout_report(workload, evidence)

    # Decision timings are measurements; everything else is a function of the inputs.
    first.pop("routing_decision_us")
    second.pop("routing_decision_us")
    assert first == second


async def test_holdout_runs_are_refused_as_evidence(tmp_path: Path) -> None:
    holdout = await simulated_runs(TICKETS_HOLDOUT)
    paths = []
    for (_, model), [run] in holdout.items():
        path = tmp_path / f"{model}.json"
        path.write_text(json.dumps(run.to_json()))
        paths.append(str(path))

    code = main(
        [
            *("--config", str(CONFIG), "--alias", "classify"),
            *("--dataset", str(TICKETS_HOLDOUT), "--results", *paths),
            *("--evidence-dataset", str(TICKETS_HOLDOUT), "--evidence", *paths),
            "--allow-simulated",
        ]
    )
    assert code == 2

    with pytest.raises(ExperimentError, match="holdout runs cannot be routing evidence"):
        evidence_runs(holdout)


async def test_evidence_must_come_from_the_matching_development_partition() -> None:
    evidence = await simulated_runs(TICKETS)
    workload = await simulated_runs(TICKETS_HOLDOUT)

    with pytest.raises(ExperimentError, match="development workload"):
        compare(
            read_gateway_config(CONFIG),
            "classify",
            load_dataset(TICKETS),
            evidence,
            load_dataset(INVOICES),
            latest(evidence),
            evidence_time(evidence),
        )
    with pytest.raises(DatasetError, match="different dataset names"):
        compare(
            read_gateway_config(CONFIG),
            "classify",
            load_dataset(TICKETS_HOLDOUT),
            workload,
            load_dataset(INVOICES),
            latest(evidence),
            evidence_time(evidence),
        )


async def test_in_sample_comparisons_are_labelled() -> None:
    evidence = await simulated_runs(TICKETS)
    report = compare(
        read_gateway_config(CONFIG),
        "classify",
        load_dataset(TICKETS),
        evidence,
        load_dataset(TICKETS),
        latest(evidence),
        evidence_time(evidence),
    )

    assert report["in_sample"] is True
    assert any("In-sample" in item for item in report["limitations"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda runs: runs.pop(("sim", "medium")), "no workload results for sim/medium"),
        (lambda runs: runs[("sim", "small")].append(runs[("sim", "small")][0]), "same number"),
    ],
    ids=["missing-target", "unequal-repetitions"],
)
async def test_incomparable_workloads_are_refused(change: Any, message: str) -> None:
    evidence = await simulated_runs(TICKETS)
    workload = await simulated_runs(TICKETS_HOLDOUT)
    change(workload)

    with pytest.raises(ExperimentError, match=message):
        holdout_report(workload, evidence)


async def test_workloads_must_use_the_aliases_generation_settings() -> None:
    evidence = await simulated_runs(TICKETS)
    longer = await simulated_runs(TICKETS_HOLDOUT, max_tokens=16)
    mixed = await simulated_runs(TICKETS_HOLDOUT)
    small = mixed[("sim", "small")][0]
    mixed[("sim", "small")] = [
        dataclasses.replace(small, identity=dataclasses.replace(small.identity, stream=True))
    ]

    with pytest.raises(ExperimentError, match="alias 'classify' sends 8"):
        holdout_report(longer, evidence)
    with pytest.raises(ExperimentError, match="different max_tokens or streaming"):
        holdout_report(mixed, evidence)


async def test_repetitions_report_variability() -> None:
    evidence = await simulated_runs(TICKETS)
    first = await simulated_runs(TICKETS_HOLDOUT)
    # A second run where each model behaves differently, as a nondeterministic one might.
    second = await simulated_runs(TICKETS_HOLDOUT, {"small": 0.6, "medium": 0.99, "large": 0.9})
    workload = {target: first[target] + second[target] for target in first}

    report = holdout_report(workload, evidence)

    assert report["workload"]["repetitions"] == 2
    variability = report["variability"]
    assert variability is not None
    assert all(0 <= value["case_agreement"] < 1 for value in variability.values())
    quality = report["policies"]["quality_constrained"]
    assert quality["requests"] == 120
    assert quality["success_rate_95ci"] is None
    assert len(quality["repetitions"]) == 2
    assert quality["quality_vs_fixed"]["difference_95ci"] is None
    assert len(quality["quality_vs_fixed"]["repetitions"]) == 2


async def test_a_policy_below_the_threshold_is_reported_as_failing() -> None:
    evidence = await simulated_runs(TICKETS)
    workload = await simulated_runs(TICKETS_HOLDOUT, {"small": 0.5, "medium": 0.93, "large": 0.98})

    report = holdout_report(workload, evidence)

    lowest = report["policies"]["lowest_cost"]
    assert lowest["routed_to"] == {"sim/small": 60}
    assert lowest["quality_threshold"] == "fails"
    assert lowest["quality_vs_fixed"]["difference"] < 0
    assert lowest["quality_vs_fixed"]["difference_95ci"][1] < 0


async def test_unqualified_routes_are_counted_or_refused() -> None:
    weak = {"small": 0.5, "medium": 0.5, "large": 0.98}
    evidence = await simulated_runs(TICKETS, weak)
    workload = await simulated_runs(TICKETS_HOLDOUT, weak)
    config = read_gateway_config(CONFIG)
    # Only the primary qualifies at 0.75, so require more than it can show.
    strict = config.models["classify"].routing.model_copy(update={"min_success_rate": 0.99})
    primary = config.model_copy(
        update={
            "models": {
                **config.models,
                "classify": config.models["classify"].model_copy(update={"routing": strict}),
            }
        }
    )
    reject = primary.model_copy(
        update={
            "models": {
                **primary.models,
                "classify": primary.models["classify"].model_copy(
                    update={
                        "routing": strict.model_copy(
                            update={"on_insufficient_evidence": InsufficientEvidence.REJECT}
                        )
                    }
                ),
            }
        }
    )

    unverified = holdout_report(workload, evidence, primary)["policies"]["quality_constrained"]
    refused = holdout_report(workload, evidence, reject)["policies"]["quality_constrained"]

    assert unverified["unverified_fallbacks"] == 60
    assert unverified["routed_to"] == {"sim/large": 60}
    assert refused["refused"] == {"no_eligible_route": 60}
    assert refused["success_rate"] == 0
    assert refused["savings_vs_fixed"]["percent"] is None
    assert "refused" in refused["savings_vs_fixed"]["reason"]


async def test_unknown_costs_are_reported_separately_with_a_range() -> None:
    evidence = await simulated_runs(TICKETS)
    workload = await simulated_runs(TICKETS_HOLDOUT)
    dataset: Dataset = load_dataset(TICKETS_HOLDOUT)
    no_usage = FakeProvider(
        "sim", completion=Completion(content="billing", finish_reason="stop", usage=None)
    )
    workload[("sim", "medium")] = [
        await run_evaluation(
            no_usage, "medium", dataset, max_output_tokens=8, price=None, simulated=True
        )
    ]

    report = holdout_report(workload, evidence)

    quality = report["policies"]["quality_constrained"]
    assert quality["unresolved_cases"] == 60
    assert quality["observed_cost_micro_usd"] == 0
    assert quality["unresolved_max_micro_usd"] > 0
    savings = quality["savings_vs_fixed"]
    assert savings["percent"] is None
    low, high = savings["percent_range"]
    assert low < high <= 100
    assert any("unknown" in item for item in report["limitations"])


def test_savings_require_comparable_observed_costs() -> None:
    baseline = [PolicyRun("fixed", requests=2, costs=[100, 100])]
    cheaper = [PolicyRun("x", requests=2, costs=[25, 25])]
    unknown = [PolicyRun("x", requests=2, costs=[50], unresolved=1, unresolved_max_micro_usd=80)]

    assert _savings(baseline, cheaper)["percent"] == 75.0
    assert _savings(baseline, cheaper)["absolute_micro_usd"] == 150
    # The policy's true cost is between 50 and 130 against a baseline of 200.
    assert _savings(baseline, unknown)["percent_range"] == [35.0, 75.0]


def test_paired_difference_interval() -> None:
    # Agreement on every case still leaves uncertainty about the difference.
    low, high = paired_difference_interval(0, 0, 40)
    assert low < 0 < high
    assert high - low > 0.1
    low, high = paired_difference_interval(0, 10, 60)
    assert high < 0
    assert paired_difference_interval(0, 0, 0) == (-1.0, 1.0)
    with pytest.raises(ValueError, match="discordant"):
        paired_difference_interval(5, 5, 6)


async def test_cli_writes_a_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def write(runs: Runs, folder: str) -> list[str]:
        (tmp_path / folder).mkdir()
        paths = []
        for (_, model), [run] in runs.items():
            path = tmp_path / folder / f"{model}.json"
            path.write_text(json.dumps(run.to_json()))
            paths.append(str(path))
        return paths

    evidence = write(await simulated_runs(TICKETS), "dev")
    workload = write(await simulated_runs(TICKETS_HOLDOUT), "holdout")

    code = main(
        [
            *("--config", str(CONFIG), "--alias", "classify"),
            *("--dataset", str(TICKETS_HOLDOUT), "--results", *workload),
            *("--evidence-dataset", str(TICKETS), "--evidence", *evidence),
            "--allow-simulated",
            *("--output", str(tmp_path / "report.json")),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "holdout cases x 1 run(s)" in out
    assert "SIMULATED" in out
    assert json.loads((tmp_path / "report.json").read_text())["in_sample"] is False
