"""persist prospective exact-input forecast stability evidence

Revision ID: w2b7f5d9c031
Revises: v1a6e4c8b920
Create Date: 2026-08-27 09:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w2b7f5d9c031"
down_revision: str | Sequence[str] | None = "v1a6e4c8b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_forecast_stability_assignments",
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("formal_producer_behavior_id", sa.String(length=64), nullable=False),
        sa.Column("information_cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completion_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_index(
        "ix_context_forecast_stability_behavior_cutoff",
        "context_forecast_stability_assignments",
        ["policy_version", "formal_producer_behavior_id", "information_cutoff_at"],
    )
    op.create_table(
        "context_forecast_stability_results",
        sa.Column("result_id", sa.String(length=128), nullable=False),
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("replica_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["context_forecast_stability_assignments.assignment_id"],
        ),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint(
            "assignment_id",
            "replica_index",
            name="uq_context_forecast_stability_replica",
        ),
    )


def downgrade() -> None:
    op.drop_table("context_forecast_stability_results")
    op.drop_index(
        "ix_context_forecast_stability_behavior_cutoff",
        table_name="context_forecast_stability_assignments",
    )
    op.drop_table("context_forecast_stability_assignments")
