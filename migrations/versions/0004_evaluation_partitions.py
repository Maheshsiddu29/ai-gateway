"""Evaluation partitions, run completeness, and per-case detail.

Revision ID: 0004_evaluation_partitions
Revises: 0003_evaluation_runs

Existing rows keep their meaning: every run recorded before this revision used a
development dataset, ran every case, and did not stream. Columns that existing
rows cannot fill are nullable. New rows must carry a results digest, enforced by
a NOT VALID check that applies to new rows only.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_evaluation_partitions"
down_revision = "0003_evaluation_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evaluation_runs",
        sa.Column("partition", sa.String(16), nullable=False, server_default="development"),
    )
    op.add_column("evaluation_runs", sa.Column("provider_base_url", sa.String(2048)))
    op.add_column(
        "evaluation_runs",
        sa.Column("stream", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("evaluation_runs", sa.Column("stop_reason", sa.String(32)))
    op.add_column("evaluation_runs", sa.Column("results_sha256", sa.String(64)))
    op.create_check_constraint(
        "evaluation_partition_known",
        "evaluation_runs",
        "partition IN ('development', 'holdout')",
    )
    op.execute(
        "ALTER TABLE evaluation_runs ADD CONSTRAINT evaluation_results_digest_required"
        " CHECK (results_sha256 IS NOT NULL) NOT VALID"
    )

    op.add_column("evaluation_case_results", sa.Column("first_token_ms", sa.Float()))
    op.add_column("evaluation_case_results", sa.Column("finish_reason", sa.String(32)))
    op.add_column("evaluation_case_results", sa.Column("unresolved_max_micro_usd", sa.BigInteger()))
    for column in ("fields_correct", "fields_predicted", "fields_expected"):
        op.add_column("evaluation_case_results", sa.Column(column, sa.Integer()))
    op.create_check_constraint(
        "evaluation_case_detail_nonnegative",
        "evaluation_case_results",
        "(first_token_ms IS NULL OR first_token_ms >= 0)"
        " AND (unresolved_max_micro_usd IS NULL OR unresolved_max_micro_usd >= 0)"
        " AND (fields_correct IS NULL OR fields_correct >= 0)"
        " AND (fields_predicted IS NULL OR fields_predicted >= 0)"
        " AND (fields_expected IS NULL OR fields_expected >= 0)",
    )


def downgrade() -> None:
    # Revision 0003 reads every non-simulated run as routing evidence, so removing
    # the partition and completeness columns would turn holdout or partial runs
    # into evidence.
    connection = op.get_bind()
    unsafe = connection.execute(
        sa.text(
            "SELECT count(*) FROM evaluation_runs"
            " WHERE partition <> 'development' OR stop_reason IS NOT NULL"
        )
    ).scalar_one()
    if unsafe:
        raise RuntimeError(
            f"{unsafe} holdout or incomplete evaluation runs would become routing evidence"
            " under revision 0003; downgrade refused"
        )
    op.drop_constraint(
        "evaluation_case_detail_nonnegative", "evaluation_case_results", type_="check"
    )
    for column in (
        "fields_expected",
        "fields_predicted",
        "fields_correct",
        "unresolved_max_micro_usd",
        "finish_reason",
        "first_token_ms",
    ):
        op.drop_column("evaluation_case_results", column)
    op.drop_constraint("evaluation_results_digest_required", "evaluation_runs", type_="check")
    op.drop_constraint("evaluation_partition_known", "evaluation_runs", type_="check")
    for column in ("results_sha256", "stop_reason", "stream", "provider_base_url", "partition"):
        op.drop_column("evaluation_runs", column)
