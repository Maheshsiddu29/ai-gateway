import json
from pathlib import Path
from typing import Any

import pytest

from gateway.core.config import read_gateway_config
from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.runner import EvaluationRun, run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from routing_experiment import PolicyReport, _savings, compare, main

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "evaluations" / "experiments" / "ticket-routing.toml"
TICKETS = ROOT / "evaluations" / "datasets" / "support-ticket-category-v1.jsonl"
INVOICES = ROOT / "evaluations" / "datasets" / "invoice-fields-v1.jsonl"
ACCURACY = {"small": 0.80, "medium": 0.93, "large": 0.98}


async def write_results(tmp_path: Path, dataset_path: Path = TICKETS) -> list[Path]:
    dataset = load_dataset(dataset_path)
    provider = SimulatedProvider("sim", dataset, ACCURACY)
    paths = []
    for model in ACCURACY:
        run = await run_evaluation(
            provider, model, dataset, max_output_tokens=8, price=None, simulated=True
        )
        path = tmp_path / f"{model}.json"
        path.write_text(json.dumps(run.to_json()))
        paths.append(path)
    return paths


def report_for(paths: list[Path]) -> dict[str, Any]:
    dataset = load_dataset(TICKETS)
    runs = {}
    for path in paths:
        run = EvaluationRun.from_json(json.loads(path.read_text()))
        runs[(run.identity.provider, run.identity.model)] = run
    now = max(run.identity.completed_at for run in runs.values())
    return compare(read_gateway_config(CONFIG), "classify", dataset, runs, runs, now)


async def test_compares_policies_on_identical_workloads(tmp_path: Path) -> None:
    report = report_for(await write_results(tmp_path))

    policies = report["policies"]
    assert report["simulated"] is True
    assert report["in_sample"] is True
    assert {name: p["served"] for name, p in policies.items()} == {
        "fixed": 44,
        "lowest_cost": 44,
        "quality_constrained": 44,
    }
    assert policies["fixed"]["routed_to"] == {"sim/large": 44}
    assert policies["lowest_cost"]["routed_to"] == {"sim/small": 44}
    assert policies["quality_constrained"]["routed_to"] == {"sim/medium": 44}
    fixed_cost = policies["fixed"]["total_cost_micro_usd"]
    quality_cost = policies["quality_constrained"]["total_cost_micro_usd"]
    assert policies["quality_constrained"]["savings_vs_fixed"]["percent"] == round(
        (fixed_cost - quality_cost) / fixed_cost * 100, 2
    )
    # Lowest cost ignores quality: its model's point rate (0.818) clears 0.75, but
    # the Wilson lower bound used by quality_constrained routing does not.
    assert policies["lowest_cost"]["below_min_success_rate"] is False
    assert policies["lowest_cost"]["success_rate"] < policies["quality_constrained"]["success_rate"]


async def test_report_is_reproducible(tmp_path: Path) -> None:
    paths = await write_results(tmp_path)

    assert report_for(paths) == report_for(paths)


def test_savings_require_comparable_observed_costs() -> None:
    baseline = PolicyReport("fixed", costs=[100, 100])
    cheaper = PolicyReport("x", costs=[25, 25])

    assert _savings(baseline, cheaper) == {"percent": 75.0, "reason": None}
    assert _savings(baseline, PolicyReport("x", costs=[50], estimated_costs=1))["percent"] is None
    refused = PolicyReport("x", costs=[1])
    refused.refused["no_eligible_route"] += 1
    assert _savings(baseline, refused)["reason"] == (
        "a policy refused requests, so the workloads differ"
    )
    assert (
        _savings(PolicyReport("fixed", costs=[0]), cheaper)["reason"] == "the baseline cost is zero"
    )


async def test_simulated_results_need_explicit_permission(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = await write_results(tmp_path)
    args = ["--config", str(CONFIG), "--alias", "classify", "--dataset", str(TICKETS), "--results"]

    assert main([*args, *map(str, paths)]) == 2
    assert "--allow-simulated" in capsys.readouterr().err
    assert main([*args, *map(str, paths), "--allow-simulated"]) == 0
    assert "SIMULATED RESULTS" in capsys.readouterr().out


async def test_results_from_another_dataset_are_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = await write_results(tmp_path, INVOICES)

    code = main(
        [
            *("--config", str(CONFIG), "--alias", "classify", "--dataset", str(TICKETS)),
            "--results",
            *map(str, paths),
            "--allow-simulated",
        ]
    )

    assert code == 2
    assert "different dataset file" in capsys.readouterr().err


async def test_every_route_target_needs_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = await write_results(tmp_path)

    code = main(
        [
            *("--config", str(CONFIG), "--alias", "classify", "--dataset", str(TICKETS)),
            "--results",
            *map(str, paths[:2]),
            "--allow-simulated",
        ]
    )

    assert code == 2
    assert "no results for sim/large" in capsys.readouterr().err


async def test_routing_benchmark_runs() -> None:
    from bench_routing import run

    results = await run(iterations=5, candidate_counts=[2])

    assert set(results["decide"]) == {"lowest/2", "quality/2"}
    assert set(results["complete"]) == {"fixed", "lowest/2", "quality/2"}
    assert all(size > 0 for size in results["decision_allocated_bytes"].values())
