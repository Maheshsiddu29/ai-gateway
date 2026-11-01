"""Benchmarks for the evaluation tooling, using deterministic local providers only.

Measures on one event loop, with no network:

* runner throughput: SimulatedProvider over the ticket holdout dataset, without
  and with a synced journal;
* scoring throughput: ``score_output`` on every holdout case of both datasets;
* report generation: ``routing_experiment.compare`` for three simulated targets;
* with ``--database-url``: ``record_run`` latency for a 60-case run, the
  idempotent re-record of the same run, and ``load_evidence`` latency once the
  table holds ``--runs`` runs. Rows are append-only, so use a throwaway database.

    uv run python tools/bench_evaluation.py --repeat 20
    uv run python tools/bench_evaluation.py --database-url postgresql+asyncpg://... --runs 500
"""

import argparse
import asyncio
import json
import platform
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.core.config import read_gateway_config
from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.journal import RunJournal
from gateway.evaluation.runner import EvaluationRun, run_evaluation
from gateway.evaluation.scoring import score_output
from gateway.evaluation.simulated import SimulatedProvider, simulated_answer
from gateway.evaluation.stats import percentile
from gateway.evaluation.store import record_run
from gateway.routing.evidence import load_evidence
from routing_experiment import compare

ROOT = Path(__file__).parents[1]
DATASETS = ROOT / "evaluations" / "datasets"
TICKETS = DATASETS / "support-ticket-category-v1.jsonl"
TICKETS_HOLDOUT = DATASETS / "support-ticket-category-v1-holdout.jsonl"
INVOICES_HOLDOUT = DATASETS / "invoice-fields-v1-holdout.jsonl"
EXPERIMENT = ROOT / "evaluations" / "experiments" / "ticket-routing.toml"
ACCURACY = {"small": 0.8, "medium": 0.93, "large": 0.98}


def milliseconds(samples: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(percentile(samples, 0.5) * 1000, 3),
        "p95_ms": round(percentile(samples, 0.95) * 1000, 3),
    }


async def timed(call: Callable[[], Any], repeat: int) -> list[float]:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        result = call()
        if asyncio.iscoroutine(result):
            await result
        samples.append(time.perf_counter() - started)
    return samples


async def runner_throughput(repeat: int) -> dict[str, Any]:
    dataset = load_dataset(TICKETS_HOLDOUT)
    provider = SimulatedProvider("sim", dataset, {"m": 0.9})

    async def plain() -> None:
        await run_evaluation(
            provider, "m", dataset, max_output_tokens=8, price=None, simulated=True, concurrency=16
        )

    with tempfile.TemporaryDirectory() as directory:

        async def journalled() -> None:
            journal = RunJournal(Path(directory) / f"{uuid.uuid4()}.jsonl", {})
            await run_evaluation(
                provider,
                "m",
                dataset,
                max_output_tokens=8,
                price=None,
                simulated=True,
                concurrency=16,
                observer=journal,
            )
            journal.discard()

        results = {}
        for name, call in (("without_journal", plain), ("with_synced_journal", journalled)):
            samples = await timed(call, repeat)
            results[name] = {
                **milliseconds(samples),
                "cases_per_second": round(len(dataset.cases) * repeat / sum(samples)),
            }
    return {"cases_per_run": len(dataset.cases), **results}


def scoring_throughput(repeat: int) -> dict[str, Any]:
    results = {}
    for path in (TICKETS_HOLDOUT, INVOICES_HOLDOUT):
        dataset = load_dataset(path)
        answers = [(case, simulated_answer(case, correct=True)) for case in dataset.cases]
        started = time.perf_counter()
        for _ in range(repeat):
            for case, answer in answers:
                score_output(case, answer)
        elapsed = time.perf_counter() - started
        results[dataset.method.value] = {
            "scores_per_second": round(len(answers) * repeat / elapsed),
        }
    return results


async def report_latency(repeat: int) -> dict[str, Any]:
    config = read_gateway_config(EXPERIMENT)
    development, holdout = load_dataset(TICKETS), load_dataset(TICKETS_HOLDOUT)

    async def runs(dataset_path: Path) -> dict[tuple[str, str], list[EvaluationRun]]:
        dataset = load_dataset(dataset_path)
        provider = SimulatedProvider("sim", dataset, ACCURACY)
        return {
            ("sim", model): [
                await run_evaluation(
                    provider, model, dataset, max_output_tokens=8, price=None, simulated=True
                )
            ]
            for model in ACCURACY
        }

    evidence = {target: target_runs[0] for target, target_runs in (await runs(TICKETS)).items()}
    workload = await runs(TICKETS_HOLDOUT)
    now = max(run.identity.completed_at for run in evidence.values())
    samples = await timed(
        lambda: compare(config, "classify", holdout, workload, development, evidence, now),
        repeat,
    )
    return {"targets": len(workload), "cases": len(holdout.cases), **milliseconds(samples)}


async def database(url: str, runs: int, repeat: int) -> dict[str, Any]:
    dataset = load_dataset(TICKETS_HOLDOUT)
    engine = create_async_engine(url)
    try:
        generated = [
            await run_evaluation(
                SimulatedProvider(f"bench{uuid.uuid4().hex[:8]}", dataset, {"m": 0.9}),
                "m",
                dataset,
                max_output_tokens=8,
                price=None,
                simulated=index % 2 == 0,
            )
            for index in range(runs)
        ]
        if not generated:
            raise ValueError("--runs must be at least 1")
        # record_run refuses runs completed after the database's clock; allow for
        # a small clock difference between this host and the database server.
        await asyncio.sleep(0.05)
        record_samples = []
        for generated_run in generated:
            started = time.perf_counter()
            await record_run(engine, generated_run)
            record_samples.append(time.perf_counter() - started)
        final = generated[-1]
        rerecord = await timed(lambda: record_run(engine, final), repeat)
        load = await timed(lambda: load_evidence(engine), repeat)
        async with engine.connect() as connection:
            rows = (
                await connection.execute(text("SELECT count(*) FROM evaluation_runs"))
            ).scalar_one()
    finally:
        await engine.dispose()
    return {
        "runs_recorded": runs,
        "evaluation_runs_rows": rows,
        "record_run_60_cases": milliseconds(record_samples),
        "rerecord_identical_run": milliseconds(rerecord),
        "load_evidence": milliseconds(load),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    results: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "repeat": args.repeat,
        "runner": await runner_throughput(args.repeat),
        "scoring": scoring_throughput(args.repeat * 10),
        "report": await report_latency(args.repeat),
    }
    if args.database_url:
        results["postgresql"] = await database(args.database_url, args.runs, args.repeat)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--database-url", help="a throwaway, migrated PostgreSQL database")
    parser.add_argument("--runs", type=int, default=200, help="runs to record with --database-url")
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
