"""Recording evaluation runs in PostgreSQL.

A run and its case results are inserted in one transaction, so routing never sees
a partial run. The tables are append-only (enforced by a trigger), and they are
separate from the inference cost ledger.

Recording is idempotent: each run stores the SHA-256 of its canonical results
file, and recording the same file again is a no-op. A different file with the
same run ID is refused, so a recorded run can never be replaced.
"""

from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.evaluation.runner import EvaluationRun


class EvaluationRecordError(Exception):
    pass


async def record_run(engine: AsyncEngine, run: EvaluationRun) -> bool:
    """Store a run; return False if this exact run was already recorded."""
    identity, summary = run.identity, run.summary
    digest = run.results_sha256()
    async with engine.begin() as connection:
        now = (await connection.execute(text("SELECT clock_timestamp()"))).scalar_one()
        # Freshness rules compare completed_at with the current time, so a run
        # cannot claim to have finished in the future.
        if identity.completed_at > now:
            raise EvaluationRecordError("the run's completion time is in the future")
        inserted = (
            await connection.execute(
                text("""INSERT INTO evaluation_runs (
                run_id, provider, model, provider_base_url, dataset, dataset_version,
                dataset_sha256, partition, task, method, evaluator_version, simulated, stream,
                max_output_tokens, pricing_version, started_at, completed_at, stop_reason,
                recorded_at, results_sha256, case_count, passed_count, invalid_count,
                error_count, mean_score, latency_samples, latency_p50_ms, latency_p95_ms,
                usage_reported_count, prompt_tokens, completion_tokens,
                estimated_cost_micro_usd, cost_complete)
                VALUES (CAST(:run_id AS uuid), :provider, :model, :provider_base_url, :dataset,
                :dataset_version, :dataset_sha256, :partition, :task, :method,
                :evaluator_version, :simulated, :stream, :max_output_tokens, :pricing_version,
                :started_at, :completed_at, :stop_reason, :recorded_at, :results_sha256,
                :case_count, :passed_count, :invalid_count, :error_count, :mean_score,
                :latency_samples, :latency_p50_ms, :latency_p95_ms, :usage_reported_count,
                :prompt_tokens, :completion_tokens, :estimated_cost_micro_usd, :cost_complete)
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id"""),
                {
                    **asdict(identity),
                    **asdict(summary),
                    "run_id": str(identity.run_id),
                    "partition": identity.partition.value,
                    "task": identity.task.value,
                    "method": identity.method.value,
                    "recorded_at": now,
                    "results_sha256": digest,
                },
            )
        ).first()
        if inserted is None:
            stored = (
                await connection.execute(
                    text(
                        "SELECT results_sha256 FROM evaluation_runs"
                        " WHERE run_id = CAST(:run_id AS uuid)"
                    ),
                    {"run_id": str(identity.run_id)},
                )
            ).scalar_one()
            if stored != digest:
                raise EvaluationRecordError(
                    f"run {identity.run_id} is already recorded with different results"
                )
            return False
        await connection.execute(
            text("""INSERT INTO evaluation_case_results (
            run_id, case_id, passed, score, invalid_response, latency_ms, first_token_ms,
            finish_reason, prompt_tokens, completion_tokens, cost_micro_usd,
            unresolved_max_micro_usd, error_code, fields_correct, fields_predicted,
            fields_expected)
            VALUES (CAST(:run_id AS uuid), :case_id, :passed, :score, :invalid_response,
            :latency_ms, :first_token_ms, :finish_reason, :prompt_tokens, :completion_tokens,
            :cost_micro_usd, :unresolved_max_micro_usd, :error_code, :fields_correct,
            :fields_predicted, :fields_expected)"""),
            [{**asdict(result), "run_id": str(identity.run_id)} for result in run.results],
        )
    return True
