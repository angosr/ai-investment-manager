"""persist deterministic product payoff projections and outcomes

Revision ID: u9f3d6a2c805
Revises: t8e2c5a1b704
Create Date: 2026-08-26 03:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u9f3d6a2c805"
down_revision: str | Sequence[str] | None = "t8e2c5a1b704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_payoff_projections",
        sa.Column("projection_id", sa.String(length=128), nullable=False),
        sa.Column("source_forecast_id", sa.String(length=128), nullable=False),
        sa.Column("economic_exposure_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["source_forecast_id"], ["forecasts.forecast_id"]),
        sa.PrimaryKeyConstraint("projection_id"),
    )
    op.create_index(
        "ix_product_payoff_projections_exposure_time",
        "product_payoff_projections",
        ["economic_exposure_id", "projected_at"],
    )
    op.create_index(
        "ix_product_payoff_projections_source_target_time",
        "product_payoff_projections",
        ["source_forecast_id", "target_id", "projected_at"],
    )
    op.create_table(
        "product_payoff_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("projection_id", sa.String(length=128), nullable=False),
        sa.Column("source_forecast_id", sa.String(length=128), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("realized_gross_bps", sa.Numeric(38, 18), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["projection_id"], ["product_payoff_projections.projection_id"]
        ),
        sa.ForeignKeyConstraint(["source_forecast_id"], ["forecasts.forecast_id"]),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint(
            "projection_id",
            "evaluation_version",
            name="uq_product_payoff_outcome_identity",
        ),
    )
    op.create_index(
        "ix_product_payoff_outcomes_cohort",
        "product_payoff_outcomes",
        ["evaluation_version", "evaluation_at", "projection_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_payoff_outcomes_cohort",
        table_name="product_payoff_outcomes",
    )
    op.drop_table("product_payoff_outcomes")
    op.drop_index(
        "ix_product_payoff_projections_source_target_time",
        table_name="product_payoff_projections",
    )
    op.drop_index(
        "ix_product_payoff_projections_exposure_time",
        table_name="product_payoff_projections",
    )
    op.drop_table("product_payoff_projections")
