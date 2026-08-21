"""add final context assessment execution ledger

Revision ID: f4b7c2d8e901
Revises: d2e6a9b4c713
Create Date: 2026-08-21 17:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b7c2d8e901"
down_revision: str | Sequence[str] | None = "d2e6a9b4c713"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assessment_executions",
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("packet_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_run_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["packet_id"], ["decision_packets.packet_id"]),
        sa.PrimaryKeyConstraint("execution_id"),
    )
    op.create_index(
        "ix_assessment_executions_behavior_completed",
        "assessment_executions",
        ["analysis_behavior_hash", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_executions_behavior_completed",
        table_name="assessment_executions",
    )
    op.drop_table("assessment_executions")
