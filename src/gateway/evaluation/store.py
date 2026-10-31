"""Recording evaluation runs in PostgreSQL.

A run and its case results are inserted in one transaction, so routing never sees
a partial run. The tables are append-only (enforced by a trigger), and they are
separate from the inference cost ledger.
"""

from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.evaluation.runner import EvaluationRun


class EvaluationRecordError(Exception):
    pass


async def record_run(engine: AsyncEngine, run: EvaluationRun) -> None:
    identity, summary = run.identity, run.summary
    async with engine.begin() as connection:
        now = (await connection.execute(text("SELECT clock_timestamp()"))).scalar_one()
        # Freshness rules compare completed_at with the current time, so a run
        # cannot claim to have finished in the future.
        if identity.completed_at > now:
            raise EvaluationRecordError("the run's completion time is in the future")
        await connection.execute(
            text("""INSERT INTO evaluation_runs (
            run_id, provider, model, dataset, dataset_version, dataset_sha256, task, method,
            evaluator_version, simulated, max_output_tokens, pricing_version, started_at,
            completed_at, recorded_at, case_count, passed_count, invalid_count, error_count,
            mean_score, latency_samples, latency_p50_ms, latency_p95_ms, usage_reported_count,
            prompt_tokens, completion_tokens, estimated_cost_micro_usd, cost_complete)
            VALUES (CAST(:run_id AS uuid), :provider, :model, :dataset, :dataset_version,
            :dataset_sha256, :task, :method, :evaluator_version, :simulated,
            :max_output_tokens, :pricing_version, :started_at, :completed_at, :recorded_at,
            :case_count, :passed_count, :invalid_count, :error_count, :mean_score,
            :latency_samples, :latency_p50_ms, :latency_p95_ms, :usage_reported_count,
            :prompt_tokens, :completion_tokens, :estimated_cost_micro_usd, :cost_complete)"""),
            {
                **asdict(identity),
                **asdict(summary),
                "run_id": str(identity.run_id),
                "task": identity.task.value,
                "method": identity.method.value,
                "recorded_at": now,
            },
        )
        await connection.execute(
            text("""INSERT INTO evaluation_case_results (
            run_id, case_id, passed, score, invalid_response, latency_ms, prompt_tokens,
            completion_tokens, cost_micro_usd, error_code)
            VALUES (CAST(:run_id AS uuid), :case_id, :passed, :score, :invalid_response,
            :latency_ms, :prompt_tokens, :completion_tokens, :cost_micro_usd, :error_code)"""),
            [{**asdict(result), "run_id": str(identity.run_id)} for result in run.results],
        )
