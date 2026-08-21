"""sleeve decision ledger

Revision ID: 9d2f6a1c4b80
Revises: f4c8e1a7b302
Create Date: 2026-08-21 04:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d2f6a1c4b80"
down_revision: str | Sequence[str] | None = "f4c8e1a7b302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_account_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint("cycle_id", "portfolio_id", name="uq_portfolio_account_cycle"),
        sa.UniqueConstraint("snapshot_hash"),
    )
    op.create_index(
        "ix_portfolio_account_as_of",
        "portfolio_account_snapshots",
        ["portfolio_id", "as_of"],
        unique=False,
    )
    op.create_table(
        "portfolio_targets",
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("account_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["portfolio_account_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("target_id"),
        sa.UniqueConstraint("cycle_id"),
        sa.UniqueConstraint("target_hash"),
    )
    op.create_index(
        "ix_portfolio_targets_valid_until",
        "portfolio_targets",
        ["valid_until", "target_id"],
        unique=False,
    )
    op.create_table(
        "portfolio_target_forecasts",
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["forecast_id"],
            ["forecasts.forecast_id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["portfolio_targets.target_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("target_id", "forecast_id"),
    )
    op.create_table(
        "portfolio_risk_decisions",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("approved_target_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["portfolio_targets.target_id"]),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("approved_target_id"),
        sa.UniqueConstraint("decision_hash"),
        sa.UniqueConstraint("target_id"),
    )
    op.create_index(
        "ix_portfolio_risk_decisions_outcome",
        "portfolio_risk_decisions",
        ["outcome", "decided_at"],
        unique=False,
    )
    op.create_table(
        "trade_plans",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("approved_target_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_target_id"],
            ["portfolio_risk_decisions.approved_target_id"],
        ),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("approved_target_id"),
        sa.UniqueConstraint("cycle_id"),
        sa.UniqueConstraint("plan_hash"),
    )
    op.create_index(
        "ix_trade_plans_created_at",
        "trade_plans",
        ["created_at", "plan_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_trade_plans_created_at", table_name="trade_plans")
    op.drop_table("trade_plans")
    op.drop_index(
        "ix_portfolio_risk_decisions_outcome",
        table_name="portfolio_risk_decisions",
    )
    op.drop_table("portfolio_risk_decisions")
    op.drop_table("portfolio_target_forecasts")
    op.drop_index("ix_portfolio_targets_valid_until", table_name="portfolio_targets")
    op.drop_table("portfolio_targets")
    op.drop_index(
        "ix_portfolio_account_as_of",
        table_name="portfolio_account_snapshots",
    )
    op.drop_table("portfolio_account_snapshots")
