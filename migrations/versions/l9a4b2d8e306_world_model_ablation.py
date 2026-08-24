"""world model prospective paired ablation

Revision ID: l9a4b2d8e306
Revises: k8f3a1c7d205
Create Date: 2026-08-24 18:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l9a4b2d8e306"
down_revision: str | Sequence[str] | None = "k8f3a1c7d205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "world_model_ablation_assignments",
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("formal_forecast_id", sa.String(length=128), nullable=False),
        sa.Column("decision_slot_id", sa.String(length=128), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completion_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("control_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["formal_forecast_id"], ["forecasts.forecast_id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["evaluation_plans.plan_id"]),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_index(
        "uq_world_model_ablation_plan_forecast",
        "world_model_ablation_assignments",
        ["plan_id", "formal_forecast_id"],
        unique=True,
    )
    op.create_index(
        "ix_world_model_ablation_plan_slot",
        "world_model_ablation_assignments",
        ["plan_id", "evaluation_at"],
        unique=False,
    )
    op.create_table(
        "world_model_ablation_results",
        sa.Column("result_id", sa.String(length=128), nullable=False),
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["world_model_ablation_assignments.assignment_id"],
        ),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint("assignment_id"),
    )


def downgrade() -> None:
    op.drop_table("world_model_ablation_results")
    op.drop_index(
        "ix_world_model_ablation_plan_slot",
        table_name="world_model_ablation_assignments",
    )
    op.drop_index(
        "uq_world_model_ablation_plan_forecast",
        table_name="world_model_ablation_assignments",
    )
    op.drop_table("world_model_ablation_assignments")
