"""Loading routing evidence from PostgreSQL; set GATEWAY_TEST_DATABASE_URL."""

import dataclasses
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gateway.core.metrics import Metrics
from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.runner import EvaluationRun, run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.store import record_run
from gateway.routing.evidence import EvidenceCache, load_evidence

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


async def measured_run(
    provider: str,
    accuracy: float,
    *,
    age: timedelta = timedelta(0),
    simulated: bool = False,
    evaluator_version: str | None = None,
) -> EvaluationRun:
    """A run presented as a real measurement, as tests need non-simulated evidence."""
    dataset = load_dataset(TICKETS)
    run = await run_evaluation(
        SimulatedProvider(provider, dataset, {"m": accuracy}),
        "m",
        dataset,
        max_output_tokens=8,
        price=None,
        simulated=False,
    )
    return dataclasses.replace(
        run,
        identity=dataclasses.replace(
            run.identity,
            started_at=run.identity.started_at - age,
            completed_at=run.identity.completed_at - age,
            simulated=simulated,
            evaluator_version=evaluator_version or run.identity.evaluator_version,
        ),
    )


async def test_loads_the_latest_qualifying_run_per_model(engine: AsyncEngine) -> None:
    provider = f"p{uuid.uuid4().hex[:8]}"
    older = await measured_run(provider, 1.0, age=timedelta(days=2))
    latest = await measured_run(provider, 0.5, age=timedelta(days=1))
    simulated = await measured_run(provider, 1.0, simulated=True)
    outdated_scorer = await measured_run(provider, 1.0, evaluator_version="exact_label-0")
    for run in (older, latest, simulated, outdated_scorer):
        await record_run(engine, run)

    snapshot = await load_evidence(engine)

    evidence = snapshot.runs[(provider, "m", "support-ticket-category", "1")]
    # The newer simulated and old-scorer runs never count.
    assert evidence.run_id == latest.identity.run_id
    assert evidence.passed_count == latest.summary.passed_count
    assert evidence.case_count == 44
    assert evidence.latency_samples == 44
    assert evidence.expected_output_tokens is not None
    assert snapshot.version != "empty"


async def test_failed_refresh_keeps_the_previous_snapshot(engine: AsyncEngine) -> None:
    provider = f"p{uuid.uuid4().hex[:8]}"
    await record_run(engine, await measured_run(provider, 1.0))
    metrics = Metrics()
    cache = EvidenceCache(engine, metrics)
    assert await cache.refresh()
    loaded = cache.current

    await engine.dispose()
    unreachable = create_async_engine("postgresql+asyncpg://x:y@127.0.0.1:1/none")
    broken = EvidenceCache(unreachable, metrics)
    broken.current = loaded

    try:
        assert not await broken.refresh()
    finally:
        await unreachable.dispose()
    assert broken.current is loaded
    assert (
        metrics.registry.get_sample_value(
            "gateway_routing_evidence_refreshes_total", {"outcome": "failed"}
        )
        == 1
    )
    assert (
        metrics.registry.get_sample_value(
            "gateway_routing_evidence_refreshes_total", {"outcome": "loaded"}
        )
        == 1
    )
