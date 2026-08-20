"""Database tables owned by analysis scheduling."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
)

from investment_manager.platform.database import metadata

analysis_trigger_events = Table(
    "analysis_trigger_events",
    metadata,
    Column("trigger_id", String(128), primary_key=True),
    Column("trigger_type", String(32), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("pipeline_id", String(128), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("priority", Integer, nullable=False),
    Column("dedup_key", String(256), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "trigger_type",
        "symbol",
        "pipeline_id",
        "dedup_key",
        name="uq_analysis_trigger_dedup",
    ),
)
Index(
    "ix_analysis_trigger_scope_time",
    analysis_trigger_events.c.symbol,
    analysis_trigger_events.c.pipeline_id,
    analysis_trigger_events.c.observed_at,
)

analysis_trigger_batches = Table(
    "analysis_trigger_batches",
    metadata,
    Column("batch_id", String(128), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("pipeline_id", String(128), nullable=False),
    Column("plan_revision", Integer, nullable=False),
    Column("first_occurred_at", DateTime(timezone=True), nullable=False),
    Column("first_observed_at", DateTime(timezone=True), nullable=False),
    Column("batched_at", DateTime(timezone=True), nullable=False),
    Column("analysis_submitted_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_analysis_trigger_batch_timing",
    analysis_trigger_batches.c.pipeline_id,
    analysis_trigger_batches.c.analysis_submitted_at,
)

analysis_call_admissions = Table(
    "analysis_call_admissions",
    metadata,
    Column("batch_id", String(128), primary_key=True),
    Column("pipeline_id", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("admitted_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_analysis_call_admissions_admitted_at", analysis_call_admissions.c.admitted_at)

trigger_outbox = Table(
    "trigger_outbox",
    metadata,
    Column("outbox_id", String(128), primary_key=True),
    Column("aggregate_key", String(256), nullable=False),
    Column("message_kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_trigger_outbox_pending",
    trigger_outbox.c.status,
    trigger_outbox.c.available_at,
    trigger_outbox.c.outbox_id,
)

analysis_trigger_plans = Table(
    "analysis_trigger_plans",
    metadata,
    Column("plan_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("pipeline_id", String(128), nullable=False),
    Column("manifest_id", String(128), nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("applied_patch_id", String(128), nullable=True, unique=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    PrimaryKeyConstraint("plan_id", "revision"),
)
Index(
    "uq_analysis_trigger_plan_current",
    analysis_trigger_plans.c.plan_id,
    unique=True,
    postgresql_where=analysis_trigger_plans.c.is_current.is_(True),
    sqlite_where=analysis_trigger_plans.c.is_current.is_(True),
)
Index(
    "uq_analysis_trigger_plan_scope_current",
    analysis_trigger_plans.c.symbol,
    analysis_trigger_plans.c.pipeline_id,
    unique=True,
    postgresql_where=analysis_trigger_plans.c.is_current.is_(True),
    sqlite_where=analysis_trigger_plans.c.is_current.is_(True),
)

analysis_scheduled_wakeups = Table(
    "analysis_scheduled_wakeups",
    metadata,
    Column("plan_id", String(128), nullable=False),
    Column("plan_revision", Integer, nullable=False),
    Column("wakeup_id", String(128), nullable=False),
    Column("wake_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    PrimaryKeyConstraint("plan_id", "plan_revision", "wakeup_id"),
    ForeignKeyConstraint(
        ["plan_id", "plan_revision"],
        ["analysis_trigger_plans.plan_id", "analysis_trigger_plans.revision"],
    ),
)
Index(
    "ix_analysis_scheduled_wakeup_due",
    analysis_scheduled_wakeups.c.wake_at,
    analysis_scheduled_wakeups.c.expires_at,
)
