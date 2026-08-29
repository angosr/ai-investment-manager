"""drop retired cash yield table after compatible release

Revision ID: b6f1d9c4a275
Revises: z5e0c8a3f164
Create Date: 2026-08-29 01:16:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6f1d9c4a275"
down_revision: str | Sequence[str] | None = "z5e0c8a3f164"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_cash_yield_product_available",
        table_name="cash_yield_product_observations",
    )
    op.drop_table("cash_yield_product_observations")


def downgrade() -> None:
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
