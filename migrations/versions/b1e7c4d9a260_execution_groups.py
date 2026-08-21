"""recoverable execution groups

Revision ID: b1e7c4d9a260
Revises: 9d2f6a1c4b80
Create Date: 2026-08-21 05:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1e7c4d9a260"
down_revision: str | Sequence[str] | None = "9d2f6a1c4b80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_groups",
        sa.Column("group_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("sleeve_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["trade_plans.plan_id"]),
        sa.PrimaryKeyConstraint("group_id"),
        sa.UniqueConstraint(
            "plan_id",
            "sleeve_id",
            name="uq_execution_group_plan_sleeve",
        ),
    )
    op.create_index(
        "uq_execution_group_active_sleeve",
        "execution_groups",
        ["sleeve_id"],
        unique=True,
        postgresql_where=sa.text("terminal = false"),
        sqlite_where=sa.text("terminal = 0"),
    )
    op.create_index(
        "ix_execution_groups_status_updated",
        "execution_groups",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "mock_product_orders",
        sa.Column("client_order_id", sa.String(length=36), nullable=False),
        sa.Column("venue_order_id", sa.String(length=128), nullable=False),
        sa.Column("group_id", sa.String(length=128), nullable=False),
        sa.Column("execution_leg_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["execution_groups.group_id"]),
        sa.PrimaryKeyConstraint("client_order_id"),
        sa.UniqueConstraint("execution_leg_id"),
        sa.UniqueConstraint("venue_order_id"),
    )
    op.create_index(
        "ix_mock_product_orders_group",
        "mock_product_orders",
        ["group_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mock_product_orders_group", table_name="mock_product_orders")
    op.drop_table("mock_product_orders")
    op.drop_index(
        "ix_execution_groups_status_updated",
        table_name="execution_groups",
    )
    op.drop_index(
        "uq_execution_group_active_sleeve",
        table_name="execution_groups",
    )
    op.drop_table("execution_groups")
