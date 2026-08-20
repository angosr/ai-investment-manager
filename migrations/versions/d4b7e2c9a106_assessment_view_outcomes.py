"""add point-in-time assessment view outcomes

Revision ID: d4b7e2c9a106
Revises: c7f4a1d9e205
Create Date: 2026-08-20 13:25:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7e2c9a106"
down_revision: str | Sequence[str] | None = "c7f4a1d9e205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_assessments",
        sa.Column("view_count", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE context_assessments "
            "SET view_count = json_array_length(payload -> 'views')"
        )
    )
    with op.batch_alter_table("context_assessments") as batch_op:
        batch_op.alter_column(
            "view_count",
            existing_type=sa.Integer(),
            nullable=False,
        )
    op.create_table(
        "assessment_view_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("already_priced", sa.String(length=32), nullable=False),
        sa.Column("uncertainty", sa.String(length=32), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("signal_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("directional_return_bps", sa.Numeric(38, 18), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["context_assessments.assessment_id"],
        ),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint(
            "assessment_id",
            "asset",
            "horizon_minutes",
            "evaluation_version",
            name="uq_assessment_view_outcome_identity",
        ),
    )
    op.create_index(
        "ix_assessment_view_outcomes_cohort",
        "assessment_view_outcomes",
        [
            "analysis_behavior_hash",
            "asset",
            "symbol",
            "horizon_minutes",
            "evaluation_at",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_view_outcomes_cohort",
        table_name="assessment_view_outcomes",
    )
    op.drop_table("assessment_view_outcomes")
    with op.batch_alter_table("context_assessments") as batch_op:
        batch_op.drop_column("view_count")
