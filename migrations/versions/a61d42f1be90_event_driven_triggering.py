"""event driven triggering and versioned analysis plans

Revision ID: a61d42f1be90
Revises: f39a6e8c42dd
Create Date: 2026-08-18 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a61d42f1be90"
down_revision: str | Sequence[str] | None = "f39a6e8c42dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_trigger_events",
        sa.Column("trigger_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(length=256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("trigger_id"),
        sa.UniqueConstraint(
            "trigger_type",
            "symbol",
            "pipeline_id",
            "dedup_key",
            name="uq_analysis_trigger_dedup",
        ),
    )
    op.create_index(
        "ix_analysis_trigger_scope_time",
        "analysis_trigger_events",
        ["symbol", "pipeline_id", "observed_at"],
        unique=False,
    )
    op.create_table(
        "analysis_trigger_batches",
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("first_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("batched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_index(
        "ix_analysis_trigger_batch_timing",
        "analysis_trigger_batches",
        ["pipeline_id", "analysis_submitted_at"],
        unique=False,
    )
    op.create_table(
        "trigger_outbox",
        sa.Column("outbox_id", sa.String(length=128), nullable=False),
        sa.Column("aggregate_key", sa.String(length=256), nullable=False),
        sa.Column("message_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("outbox_id"),
    )
    op.create_index(
        "ix_trigger_outbox_pending",
        "trigger_outbox",
        ["status", "available_at", "outbox_id"],
        unique=False,
    )
    op.create_table(
        "analysis_trigger_plans",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("manifest_id", sa.String(length=128), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("applied_patch_id", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "revision"),
        sa.UniqueConstraint("applied_patch_id"),
    )
    op.create_index(
        "uq_analysis_trigger_plan_current",
        "analysis_trigger_plans",
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE"),
        sqlite_where=sa.text("is_current IS 1"),
    )
    op.create_table(
        "analysis_scheduled_wakeups",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("wakeup_id", sa.String(length=128), nullable=False),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "plan_revision"],
            ["analysis_trigger_plans.plan_id", "analysis_trigger_plans.revision"],
        ),
        sa.PrimaryKeyConstraint("plan_id", "plan_revision", "wakeup_id"),
    )
    op.create_index(
        "ix_analysis_scheduled_wakeup_due",
        "analysis_scheduled_wakeups",
        ["wake_at", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_scheduled_wakeup_due", table_name="analysis_scheduled_wakeups")
    op.drop_table("analysis_scheduled_wakeups")
    op.drop_index("uq_analysis_trigger_plan_current", table_name="analysis_trigger_plans")
    op.drop_table("analysis_trigger_plans")
    op.drop_index("ix_trigger_outbox_pending", table_name="trigger_outbox")
    op.drop_table("trigger_outbox")
    op.drop_index("ix_analysis_trigger_scope_time", table_name="analysis_trigger_events")
    op.drop_index("ix_analysis_trigger_batch_timing", table_name="analysis_trigger_batches")
    op.drop_table("analysis_trigger_batches")
    op.drop_table("analysis_trigger_events")
