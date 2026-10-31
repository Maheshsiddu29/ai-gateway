"""Financial accounting tables.

Revision ID: 0001_cost_ledger
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_cost_ledger"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_periods",
        sa.Column("scope", sa.String(8), primary_key=True),
        sa.Column("subject_id", sa.String(128), primary_key=True),
        sa.Column("period_kind", sa.String(8), primary_key=True),
        sa.Column("period_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("reserved_micro_usd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("spent_micro_usd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("reserved_micro_usd >= 0", name="budget_reserved_nonnegative"),
        sa.CheckConstraint("spent_micro_usd >= 0", name="budget_spent_nonnegative"),
    )
    op.create_table(
        "cost_attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("team_id", sa.String(128)),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("pricing_version", sa.String(64), nullable=False),
        sa.Column("input_rate", sa.Numeric(20, 9), nullable=False),
        sa.Column("output_rate", sa.Numeric(20, 9), nullable=False),
        sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("maximum_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("charge_micro_usd", sa.BigInteger()),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("classification", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reserved_micro_usd >= 0", name="attempt_reservation_nonnegative"),
        sa.CheckConstraint(
            "charge_micro_usd IS NULL OR charge_micro_usd >= 0", name="attempt_charge_nonnegative"
        ),
        sa.CheckConstraint(
            "state IN ('reserved','dispatched','settled','released','unresolved')",
            name="attempt_state_valid",
        ),
    )
    op.create_index(
        "ix_cost_attempts_principal_time", "cost_attempts", ["principal_id", "created_at"]
    )
    op.create_index("ix_cost_attempts_team_time", "cost_attempts", ["team_id", "created_at"])
    op.create_index("ix_cost_attempts_expiry", "cost_attempts", ["state", "expires_at"])
    op.create_table(
        "cost_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "attempt_id", sa.Uuid(), sa.ForeignKey("cost_attempts.attempt_id"), nullable=False
        ),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("amount_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "event_type", name="uq_cost_event_attempt_type"),
    )
    op.create_table(
        "attempt_budget_periods",
        sa.Column(
            "attempt_id", sa.Uuid(), sa.ForeignKey("cost_attempts.attempt_id"), primary_key=True
        ),
        sa.Column("scope", sa.String(8), primary_key=True),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("period_kind", sa.String(8), primary_key=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""CREATE FUNCTION reject_cost_event_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'cost events are append-only'; END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER cost_events_append_only
        BEFORE UPDATE OR DELETE ON cost_events
        FOR EACH ROW EXECUTE FUNCTION reject_cost_event_mutation()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER cost_events_append_only ON cost_events")
    op.execute("DROP FUNCTION reject_cost_event_mutation()")
    op.drop_table("attempt_budget_periods")
    op.drop_table("cost_events")
    op.drop_index("ix_cost_attempts_expiry", table_name="cost_attempts")
    op.drop_index("ix_cost_attempts_team_time", table_name="cost_attempts")
    op.drop_index("ix_cost_attempts_principal_time", table_name="cost_attempts")
    op.drop_table("cost_attempts")
    op.drop_table("budget_periods")
