"""add immutable context assessment evidence ledger

Revision ID: f1c7a4d8e209
Revises: c2a8f4d9e617
Create Date: 2026-08-20 10:55:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c7a4d8e209"
down_revision: str | Sequence[str] | None = "c2a8f4d9e617"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_packets",
        sa.Column("packet_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_scope", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("packet_id"),
        sa.UniqueConstraint("content_hash"),
    )
    op.create_index(
        "ix_decision_packets_scope_as_of",
        "decision_packets",
        ["analysis_scope", "as_of"],
    )
    op.create_table(
        "context_assessments",
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("packet_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_scope", sa.String(length=128), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["packet_id"], ["decision_packets.packet_id"]),
        sa.PrimaryKeyConstraint("assessment_id"),
        sa.UniqueConstraint(
            "packet_id",
            "analysis_behavior_hash",
            name="uq_context_assessment_packet_behavior",
        ),
    )
    op.create_index(
        "ix_context_assessments_behavior_available",
        "context_assessments",
        ["analysis_behavior_hash", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_assessments_behavior_available",
        table_name="context_assessments",
    )
    op.drop_table("context_assessments")
    op.drop_index("ix_decision_packets_scope_as_of", table_name="decision_packets")
    op.drop_table("decision_packets")
