"""persist prospective quant-context posterior assignments

Revision ID: x3c8a6e1d142
Revises: w2b7f5d9c031
Create Date: 2026-08-27 16:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "x3c8a6e1d142"
down_revision: str | Sequence[str] | None = "w2b7f5d9c031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_forecast_posterior_assignments",
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("producer_behavior_id", sa.String(length=64), nullable=False),
        sa.Column("quant_producer_behavior_id", sa.String(length=64), nullable=False),
        sa.Column("information_cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completion_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_index(
        "ix_context_forecast_posterior_behavior_cutoff",
        "context_forecast_posterior_assignments",
        ["policy_version", "producer_behavior_id", "information_cutoff_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_forecast_posterior_behavior_cutoff",
        table_name="context_forecast_posterior_assignments",
    )
    op.drop_table("context_forecast_posterior_assignments")
