"""candidate shadow outcomes

Revision ID: c3d8e7f4a2b1
Revises: b7c1a2e4f9d3
Create Date: 2026-08-18 16:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8e7f4a2b1"
down_revision: str | Sequence[str] | None = "b7c1a2e4f9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("net_return_bps", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["signal_candidates.candidate_id"]),
        sa.ForeignKeyConstraint(["cycle_id"], ["analysis_cycles.cycle_id"]),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint("candidate_id"),
    )
    op.create_index(
        "ix_candidate_outcomes_evaluation_at",
        "candidate_outcomes",
        ["evaluation_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_outcomes_evaluation_at", table_name="candidate_outcomes")
    op.drop_table("candidate_outcomes")
