"""preserve forecast evidence across behavior-equivalent releases

Revision ID: c2a8f4d9e617
Revises: 6a7d9f2c1b84
Create Date: 2026-08-19 22:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2a8f4d9e617"
down_revision: str | Sequence[str] | None = "6a7d9f2c1b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_forecast_outcomes") as batch:
        batch.add_column(
            sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=True)
        )
        batch.create_index(
            "ix_analysis_forecast_outcomes_behavior_evaluation",
            ["analysis_behavior_hash", "evaluation_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("analysis_forecast_outcomes") as batch:
        batch.drop_index("ix_analysis_forecast_outcomes_behavior_evaluation")
        batch.drop_column("analysis_behavior_hash")
