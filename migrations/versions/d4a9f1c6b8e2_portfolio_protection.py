"""persistent portfolio protection

Revision ID: d4a9f1c6b8e2
Revises: c3d8e7f4a2b1
Create Date: 2026-08-18 17:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a9f1c6b8e2"
down_revision: str | Sequence[str] | None = "c3d8e7f4a2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "portfolio_protection_states",
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("kill_switch_active", sa.Boolean(), nullable=False),
        sa.Column("high_water_equity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("last_equity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("drawdown_fraction", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("trip_reason", sa.String(length=128), nullable=True),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reset_reason", sa.String(length=512), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("portfolio_id"),
    )
    op.bulk_insert(
        table,
        [
            {
                "portfolio_id": "primary",
                "kill_switch_active": False,
                "high_water_equity": None,
                "last_equity": None,
                "drawdown_fraction": 0,
                "trip_reason": None,
                "tripped_at": None,
                "last_reset_at": None,
                "last_reset_reason": None,
                "updated_at": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("portfolio_protection_states")
