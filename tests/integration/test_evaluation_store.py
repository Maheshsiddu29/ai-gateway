"""Evaluation results in PostgreSQL; set GATEWAY_TEST_DATABASE_URL."""

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

TICKETS = (
    Path(__file__).parents[2] / "evaluations" / "datasets" / "support-ticket-category-v1.jsonl"
)


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


async def test_recording_twice_fails_without_partial_rows(engine: AsyncEngine) -> None:
    run = await simulated_run()
    await record_run(engine, run)

    with pytest.raises(IntegrityError):
        await record_run(engine, run)


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
