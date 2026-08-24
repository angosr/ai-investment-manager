"""capital benchmark points

Revision ID: k8f3a1c7d205
Revises: j7e1a4c9f326
Create Date: 2026-08-24 18:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k8f3a1c7d205"
down_revision: str | Sequence[str] | None = "j7e1a4c9f326"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capital_benchmark_points",
        sa.Column("point_id", sa.String(length=128), nullable=False),
        sa.Column("policy_id", sa.String(length=128), nullable=False),
        sa.Column("account_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["portfolio_account_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("point_id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_index(
        "uq_capital_benchmark_policy_account",
        "capital_benchmark_points",
        ["policy_id", "account_snapshot_id"],
        unique=True,
    )
    op.create_index(
        "ix_capital_benchmark_policy_revision",
        "capital_benchmark_points",
        ["policy_id", "revision"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capital_benchmark_policy_revision",
        table_name="capital_benchmark_points",
    )
    op.drop_index(
        "uq_capital_benchmark_policy_account",
        table_name="capital_benchmark_points",
    )
    op.drop_table("capital_benchmark_points")
