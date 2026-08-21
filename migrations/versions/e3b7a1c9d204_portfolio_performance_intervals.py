"""record causal portfolio performance intervals

Revision ID: e3b7a1c9d204
Revises: d1a4c7e9b205
Create Date: 2026-08-21 07:25:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3b7a1c9d204"
down_revision: str | Sequence[str] | None = "d1a4c7e9b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_performance_intervals",
        sa.Column("interval_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("start_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("end_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("start_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_revision", sa.Integer(), nullable=False),
        sa.Column("end_revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "return_fraction",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("interval_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["end_snapshot_id"],
            ["portfolio_account_snapshots.snapshot_id"],
        ),
        sa.ForeignKeyConstraint(
            ["start_snapshot_id"],
            ["portfolio_account_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("interval_id"),
        sa.UniqueConstraint("end_snapshot_id"),
        sa.UniqueConstraint("interval_hash"),
    )
    op.create_index(
        "ix_portfolio_performance_end",
        "portfolio_performance_intervals",
        ["portfolio_id", "end_as_of", "end_revision", "interval_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_performance_end",
        table_name="portfolio_performance_intervals",
    )
    op.drop_table("portfolio_performance_intervals")
