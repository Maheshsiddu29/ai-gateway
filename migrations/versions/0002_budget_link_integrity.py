"""Protect budget links and pricing snapshots.

Revision ID: 0002_budget_link_integrity
Revises: 0001_cost_ledger
"""

from alembic import op

revision = "0002_budget_link_integrity"
down_revision = "0001_cost_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_attempt_budget_period",
        "attempt_budget_periods",
        "budget_periods",
        ["scope", "subject_id", "period_kind", "period_start"],
        ["scope", "subject_id", "period_kind", "period_start"],
        ondelete="RESTRICT",
    )
    op.execute("""CREATE FUNCTION reject_cost_snapshot_mutation() RETURNS trigger AS $$
        BEGIN
            IF ROW(NEW.request_id, NEW.principal_id, NEW.team_id, NEW.provider,
                   NEW.model, NEW.pricing_version, NEW.input_rate, NEW.output_rate,
                   NEW.estimated_input_tokens, NEW.maximum_output_tokens,
                   NEW.reserved_micro_usd, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.request_id, OLD.principal_id, OLD.team_id, OLD.provider,
                   OLD.model, OLD.pricing_version, OLD.input_rate, OLD.output_rate,
                   OLD.estimated_input_tokens, OLD.maximum_output_tokens,
                   OLD.reserved_micro_usd, OLD.created_at)
            THEN RAISE EXCEPTION 'cost attempt snapshot is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER cost_attempt_snapshot_immutable
        BEFORE UPDATE ON cost_attempts
        FOR EACH ROW EXECUTE FUNCTION reject_cost_snapshot_mutation()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER cost_attempt_snapshot_immutable ON cost_attempts")
    op.execute("DROP FUNCTION reject_cost_snapshot_mutation()")
    op.drop_constraint("fk_attempt_budget_period", "attempt_budget_periods", type_="foreignkey")
