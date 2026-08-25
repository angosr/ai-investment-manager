"""persist point-in-time TradFi trading schedules

Revision ID: r6a9d3e2f842
Revises: q5f8c2d1e731
Create Date: 2026-08-25 09:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r6a9d3e2f842"
down_revision: str | Sequence[str] | None = "q5f8c2d1e731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tradfi_trading_schedules",
        sa.Column("schedule_id", sa.String(length=128), nullable=False),
        sa.Column("exchange_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("schedule_id"),
    )
    op.create_index(
        "ix_tradfi_trading_schedules_time",
        "tradfi_trading_schedules",
        ["exchange_time", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tradfi_trading_schedules_time",
        table_name="tradfi_trading_schedules",
    )
    op.drop_table("tradfi_trading_schedules")
