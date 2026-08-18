"""outcome window reports

Revision ID: d17e4c8b902a
Revises: c9f2e6a1d4b8
Create Date: 2026-08-18 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d17e4c8b902a"
down_revision: str | Sequence[str] | None = "c9f2e6a1d4b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outcome_window_reports",
        sa.Column("report_id", sa.String(length=128), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("pipeline_version", sa.String(length=128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index(
        "ix_outcome_window_reports_window",
        "outcome_window_reports",
        ["pipeline_version", "window_start", "window_end"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outcome_window_reports_window",
        table_name="outcome_window_reports",
    )
    op.drop_table("outcome_window_reports")
