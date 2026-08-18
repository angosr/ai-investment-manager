"""reconciliation reports

Revision ID: c9f2e6a1d4b8
Revises: 8a73c0d53f31
Create Date: 2026-08-18 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f2e6a1d4b8"
down_revision: str | Sequence[str] | None = "8a73c0d53f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_reports",
        sa.Column("report_id", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("freeze_new_risk", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index(
        "ix_reconciliation_reports_as_of",
        "reconciliation_reports",
        ["as_of"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_reports_status",
        "reconciliation_reports",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_reports_status", table_name="reconciliation_reports")
    op.drop_index("ix_reconciliation_reports_as_of", table_name="reconciliation_reports")
    op.drop_table("reconciliation_reports")
