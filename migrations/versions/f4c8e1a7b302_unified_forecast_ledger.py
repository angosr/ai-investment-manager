"""add unified forecast ledger and outcomes

Revision ID: f4c8e1a7b302
Revises: e8f2c6a1d409
Create Date: 2026-08-21 03:35:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c8e1a7b302"
down_revision: str | Sequence[str] | None = "e8f2c6a1d409"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecasts",
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("producer_version", sa.String(length=128), nullable=False),
        sa.Column("forecast_family", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_forecast_id", sa.String(length=128), nullable=True),
        sa.Column("assessment_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["context_assessments.assessment_id"],
        ),
        sa.ForeignKeyConstraint(["base_forecast_id"], ["forecasts.forecast_id"]),
        sa.PrimaryKeyConstraint("forecast_id"),
    )
    op.create_index(
        "ix_forecasts_pending_evaluation",
        "forecasts",
        ["evaluation_at", "forecast_id"],
    )
    op.create_index(
        "ix_forecasts_producer_target",
        "forecasts",
        ["producer_id", "producer_version", "target_id", "available_at"],
    )
    op.create_table(
        "forecast_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "gross_target_return_bps",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["forecast_id"], ["forecasts.forecast_id"]),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint(
            "forecast_id",
            "evaluation_version",
            name="uq_forecast_outcome_identity",
        ),
    )
    op.create_index(
        "ix_forecast_outcomes_cohort",
        "forecast_outcomes",
        ["evaluation_version", "evaluation_at", "forecast_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_forecast_outcomes_cohort",
        table_name="forecast_outcomes",
    )
    op.drop_table("forecast_outcomes")
    op.drop_index(
        "ix_forecasts_producer_target",
        table_name="forecasts",
    )
    op.drop_index(
        "ix_forecasts_pending_evaluation",
        table_name="forecasts",
    )
    op.drop_table("forecasts")
