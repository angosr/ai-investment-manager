"""persist read-only cash yield product observations

Revision ID: y4d9b7f2e253
Revises: x3c8a6e1d142
Create Date: 2026-08-28 22:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "y4d9b7f2e253"
down_revision: str | Sequence[str] | None = "x3c8a6e1d142"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cash_yield_product_observations",
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("asset", sa.String(length=32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint("observation_hash"),
    )
    op.create_index(
        "ix_cash_yield_product_available",
        "cash_yield_product_observations",
        ["product_id", "asset", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cash_yield_product_available",
        table_name="cash_yield_product_observations",
    )
    op.drop_table("cash_yield_product_observations")
