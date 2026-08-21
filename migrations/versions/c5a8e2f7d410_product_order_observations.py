"""product order observations

Revision ID: c5a8e2f7d410
Revises: b1e7c4d9a260
Create Date: 2026-08-21 05:28:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a8e2f7d410"
down_revision: str | Sequence[str] | None = "b1e7c4d9a260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_order_observations",
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("observation_hash", sa.String(length=64), nullable=False),
        sa.Column("client_order_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=128), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["execution_groups.group_id"]),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint("observation_hash"),
    )
    op.create_index(
        "ix_product_order_observations_group_available",
        "product_order_observations",
        ["group_id", "available_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_order_observations_group_available",
        table_name="product_order_observations",
    )
    op.drop_table("product_order_observations")
