"""Offline evaluation results used as routing evidence.

Revision ID: 0003_evaluation_runs
Revises: 0002_budget_link_integrity
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_evaluation_runs"
down_revision = "0002_budget_link_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("dataset_version", sa.String(32), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("evaluator_version", sa.String(32), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("pricing_version", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("invalid_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("mean_score", sa.Float(), nullable=False),
        sa.Column("latency_samples", sa.Integer(), nullable=False),
        sa.Column("latency_p50_ms", sa.Float()),
        sa.Column("latency_p95_ms", sa.Float()),
        sa.Column("usage_reported_count", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("cost_complete", sa.Boolean(), nullable=False),
        sa.CheckConstraint("case_count > 0", name="evaluation_cases_positive"),
        sa.CheckConstraint(
            "passed_count BETWEEN 0 AND case_count"
            " AND invalid_count BETWEEN 0 AND case_count"
            " AND error_count BETWEEN 0 AND case_count"
            " AND latency_samples BETWEEN 0 AND case_count"
            " AND usage_reported_count BETWEEN 0 AND case_count",
            name="evaluation_counts_bounded",
        ),
        sa.CheckConstraint("mean_score BETWEEN 0 AND 1", name="evaluation_score_bounded"),
        sa.CheckConstraint(
            "started_at <= completed_at AND completed_at <= recorded_at",
            name="evaluation_times_ordered",
        ),
    )
    op.create_index(
        "ix_evaluation_runs_evidence",
        "evaluation_runs",
        ["provider", "model", "dataset", "dataset_version", "completed_at"],
    )
    op.create_table(
        "evaluation_case_results",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("evaluation_runs.run_id"), primary_key=True),
        sa.Column("case_id", sa.String(64), primary_key=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("invalid_response", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("cost_micro_usd", sa.BigInteger()),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint("score BETWEEN 0 AND 1", name="evaluation_case_score_bounded"),
    )
    op.execute("""CREATE FUNCTION reject_evaluation_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'evaluation results are append-only'; END;
        $$ LANGUAGE plpgsql""")
    for table in ("evaluation_runs", "evaluation_case_results"):
        op.execute(f"""CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_evaluation_mutation()""")


def downgrade() -> None:
    for table in ("evaluation_case_results", "evaluation_runs"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_evaluation_mutation()")
    op.drop_table("evaluation_case_results")
    op.drop_index("ix_evaluation_runs_evidence", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
