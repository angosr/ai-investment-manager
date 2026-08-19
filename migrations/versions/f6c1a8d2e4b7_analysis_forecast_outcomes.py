"""analysis forecast outcomes

Revision ID: f6c1a8d2e4b7
Revises: e5b0c2d7a9f4
Create Date: 2026-08-19 11:55:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c1a8d2e4b7"
down_revision: str | Sequence[str] | None = "e5b0c2d7a9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_forecast_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("proposal_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("pipeline_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "directional_return_bps",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["cycle_id"], ["analysis_cycles.cycle_id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["analysis_proposals.proposal_id"]),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint("proposal_id"),
    )
    op.create_index(
        "ix_analysis_forecast_outcomes_pipeline_evaluation",
        "analysis_forecast_outcomes",
        ["pipeline_version", "evaluation_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_forecast_outcomes_pipeline_evaluation",
        table_name="analysis_forecast_outcomes",
    )
    op.drop_table("analysis_forecast_outcomes")
