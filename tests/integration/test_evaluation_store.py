"""Evaluation results in PostgreSQL; set GATEWAY_TEST_DATABASE_URL."""

import asyncio
import dataclasses
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.runner import EvaluationRun, run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.store import EvaluationRecordError, record_run

DATASETS = Path(__file__).parents[2] / "evaluations" / "datasets"
TICKETS = DATASETS / "support-ticket-category-v1.jsonl"
INVOICES_HOLDOUT = DATASETS / "invoice-fields-v1-holdout.jsonl"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    result = create_async_engine(url)
    try:
        yield result
    finally:
        await result.dispose()


async def simulated_run(model: str = "m") -> EvaluationRun:
    dataset = load_dataset(TICKETS)
    return await run_evaluation(
        SimulatedProvider("sim", dataset, {model: 0.8}),
        model,
        dataset,
        max_output_tokens=8,
        price=None,
        simulated=True,
    )


async def test_records_a_run_and_its_cases_atomically(engine: AsyncEngine) -> None:
    run = await simulated_run()

    await record_run(engine, run)

    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT * FROM evaluation_runs WHERE run_id=:id"),
                    {"id": run.identity.run_id},
                )
            )
            .mappings()
            .one()
        )
        cases = (
            await connection.execute(
                text("""SELECT COUNT(*), SUM(passed::int) FROM evaluation_case_results
                    WHERE run_id=:id"""),
                {"id": run.identity.run_id},
            )
        ).one()
    assert row["simulated"] is True
    assert row["dataset_sha256"] == run.identity.dataset_sha256
    assert row["evaluator_version"] == run.identity.evaluator_version
    assert row["passed_count"] == run.summary.passed_count
    assert row["completed_at"] <= row["recorded_at"]
    assert tuple(cases) == (run.summary.case_count, run.summary.passed_count)


async def case_rows(engine: AsyncEngine, run: EvaluationRun) -> int:
    async with engine.connect() as connection:
        count: int = (
            await connection.execute(
                text("SELECT COUNT(*) FROM evaluation_case_results WHERE run_id=:id"),
                {"id": run.identity.run_id},
            )
        ).scalar_one()
    return count


async def test_recording_the_same_run_again_is_a_no_op(engine: AsyncEngine) -> None:
    run = await simulated_run()

    assert await record_run(engine, run) is True
    assert await record_run(engine, run) is False
    assert await case_rows(engine, run) == run.summary.case_count


async def test_a_different_run_with_the_same_id_is_refused(engine: AsyncEngine) -> None:
    run = await simulated_run()
    await record_run(engine, run)
    other = await simulated_run(model="other")
    forged = dataclasses.replace(other, identity=dataclasses.replace(run.identity, model="other"))

    with pytest.raises(EvaluationRecordError, match="already recorded with different results"):
        await record_run(engine, forged)
    assert await case_rows(engine, run) == run.summary.case_count


async def test_concurrent_recording_stores_one_copy(engine: AsyncEngine) -> None:
    run = await simulated_run()

    outcomes = await asyncio.gather(*(record_run(engine, run) for _ in range(5)))

    assert sorted(outcomes) == [False, False, False, False, True]
    assert await case_rows(engine, run) == run.summary.case_count


async def test_partition_stream_digest_and_case_detail_are_stored(engine: AsyncEngine) -> None:
    dataset = load_dataset(INVOICES_HOLDOUT)
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 0.5}),
        "m",
        dataset,
        max_output_tokens=64,
        price=None,
        simulated=True,
        stream=True,
    )

    await record_run(engine, run)

    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("""SELECT partition, stream, stop_reason, results_sha256,
                        provider_base_url FROM evaluation_runs WHERE run_id=:id"""),
                    {"id": run.identity.run_id},
                )
            )
            .mappings()
            .one()
        )
        detail = (
            await connection.execute(
                text("""SELECT SUM(fields_correct), SUM(fields_expected),
                    COUNT(first_token_ms), COUNT(*) FILTER (WHERE finish_reason = 'stop')
                    FROM evaluation_case_results WHERE run_id=:id"""),
                {"id": run.identity.run_id},
            )
        ).one()
    assert dict(row) == {
        "partition": "holdout",
        "stream": True,
        "stop_reason": None,
        "results_sha256": run.results_sha256(),
        "provider_base_url": None,
    }
    expected_fields = sum(result.fields_expected or 0 for result in run.results)
    assert tuple(detail) == (
        sum(result.fields_correct or 0 for result in run.results),
        expected_fields,
        run.summary.case_count,
        run.summary.case_count,
    )


async def test_new_runs_require_a_results_digest(engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="evaluation_results_digest_required"):
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO evaluation_runs (run_id, provider, model, dataset,
                    dataset_version, dataset_sha256, task, method, evaluator_version, simulated,
                    max_output_tokens, started_at, completed_at, recorded_at, case_count,
                    passed_count, invalid_count, error_count, mean_score, latency_samples,
                    usage_reported_count, prompt_tokens, completion_tokens,
                    estimated_cost_micro_usd, cost_complete)
                    VALUES (gen_random_uuid(), 'p', 'm', 'd', '1', 'x', 'classification',
                    'exact_label', 'v', true, 1, now(), now(), now(), 1, 0, 0, 0, 0, 0, 0, 0, 0,
                    0, true)"""),
            )


async def test_results_are_append_only(engine: AsyncEngine) -> None:
    run = await simulated_run()
    await record_run(engine, run)

    for statement in (
        "UPDATE evaluation_runs SET passed_count=case_count WHERE run_id=:id",
        "DELETE FROM evaluation_case_results WHERE run_id=:id",
        "UPDATE evaluation_case_results SET passed=true WHERE run_id=:id",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as connection:
                await connection.execute(text(statement), {"id": run.identity.run_id})


async def test_future_completion_times_are_rejected(engine: AsyncEngine) -> None:
    run = await simulated_run()
    future = dataclasses.replace(
        run,
        identity=dataclasses.replace(
            run.identity, completed_at=datetime.now(UTC) + timedelta(days=1)
        ),
    )

    with pytest.raises(EvaluationRecordError, match="future"):
        await record_run(engine, future)


async def test_database_constraints_reject_inconsistent_counts(engine: AsyncEngine) -> None:
    run = await simulated_run()
    bad = dataclasses.replace(
        run, summary=dataclasses.replace(run.summary, passed_count=run.summary.case_count + 1)
    )

    with pytest.raises(IntegrityError, match="evaluation_counts_bounded"):
        await record_run(engine, bad)


async def test_database_rejects_negative_latency(engine: AsyncEngine) -> None:
    run = await simulated_run()
    bad = dataclasses.replace(run, summary=dataclasses.replace(run.summary, latency_p95_ms=-1.0))

    with pytest.raises(IntegrityError, match="evaluation_measurements_nonnegative"):
        await record_run(engine, bad)
