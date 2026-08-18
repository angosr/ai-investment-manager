"""persistent mock exchange

Revision ID: 8a73c0d53f31
Revises: 5bc92ed9ec70
Create Date: 2026-08-18 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a73c0d53f31"
down_revision: str | Sequence[str] | None = "5bc92ed9ec70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mock_exchange_orders",
        sa.Column("client_order_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("client_order_id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index(
        "ix_mock_exchange_orders_observed_at",
        "mock_exchange_orders",
        ["observed_at"],
        unique=False,
    )
    op.create_table(
        "mock_exchange_protections",
        sa.Column("protection_id", sa.String(length=128), nullable=False),
        sa.Column("position_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("protection_id"),
        sa.UniqueConstraint("position_id"),
    )


def downgrade() -> None:
    op.drop_table("mock_exchange_protections")
    op.drop_index(
        "ix_mock_exchange_orders_observed_at",
        table_name="mock_exchange_orders",
    )
    op.drop_table("mock_exchange_orders")
