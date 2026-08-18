"""Temporal owns workflow state

Revision ID: 7f4b9f48bb32
Revises: e90def444b1f
Create Date: 2026-08-18 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7f4b9f48bb32"
down_revision: str | Sequence[str] | None = "e90def444b1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("analysis_workflow_runs")


def downgrade() -> None:
    op.create_table(
        "analysis_workflow_runs",
        sa.Column("workflow_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("workflow_id"),
        sa.UniqueConstraint("cycle_id"),
    )
