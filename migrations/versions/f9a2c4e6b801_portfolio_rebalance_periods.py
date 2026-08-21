"""freeze one authoritative portfolio rebalance decision per period

Revision ID: f9a2c4e6b801
Revises: e3b7a1c9d204
Create Date: 2026-08-21 08:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a2c4e6b801"
down_revision: str | Sequence[str] | None = "e3b7a1c9d204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_rebalance_period_start",
        table_name="portfolio_rebalance_periods",
    )
    op.drop_table("portfolio_rebalance_periods")
