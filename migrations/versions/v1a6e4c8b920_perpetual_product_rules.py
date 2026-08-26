"""persist point-in-time perpetual product rules

Revision ID: v1a6e4c8b920
Revises: u9f3d6a2c805
Create Date: 2026-08-26 03:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v1a6e4c8b920"
down_revision: str | Sequence[str] | None = "u9f3d6a2c805"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perpetual_product_rules",
        sa.Column("rules_id", sa.String(length=128), nullable=False),
        sa.Column("instrument_id", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("rules_id"),
    )
    op.create_index(
        "ix_perpetual_product_rules_instrument_time",
        "perpetual_product_rules",
        ["instrument_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_perpetual_product_rules_instrument_time",
        table_name="perpetual_product_rules",
    )
    op.drop_table("perpetual_product_rules")
