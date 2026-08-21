"""remove the redundant account-level cadence ledger

Revision ID: c8f1a4d7e209
Revises: fa3d7e9b1204
Create Date: 2026-08-21 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f1a4d7e209"
down_revision: str | Sequence[str] | None = "fa3d7e9b1204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_portfolio_rebalance_period_start",
        table_name="portfolio_rebalance_periods",
    )
    op.drop_table("portfolio_rebalance_periods")
    op.create_table(
        "capital_cycle_records",
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("cause_id", sa.String(length=128), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_cycle_id", sa.String(length=128), nullable=False),
        sa.Column("account_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["portfolio_account_snapshots.snapshot_id"],
        ),
        sa.ForeignKeyConstraint(["target_id"], ["portfolio_targets.target_id"]),
        sa.PrimaryKeyConstraint("record_id"),
        sa.UniqueConstraint(
            "portfolio_id",
            "pipeline_id",
            "cause_id",
            name="uq_capital_cycle_cause",
        ),
    )
    op.create_index(
        "ix_capital_cycle_records_time",
        "capital_cycle_records",
        ["pipeline_id", "evaluated_at", "record_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capital_cycle_records_time",
        table_name="capital_cycle_records",
    )
    op.drop_table("capital_cycle_records")
    op.create_table(
        "portfolio_rebalance_periods",
        sa.Column("period_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("candidate_forecast_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_forecast_id"],
            ["forecasts.forecast_id"],
        ),
        sa.PrimaryKeyConstraint("period_id"),
        sa.UniqueConstraint(
            "portfolio_id",
            "policy_version",
            "period_start",
            name="uq_portfolio_rebalance_period",
        ),
    )
    op.create_index(
        "ix_portfolio_rebalance_period_start",
        "portfolio_rebalance_periods",
        ["portfolio_id", "period_start"],
    )
