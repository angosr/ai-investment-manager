"""Database tables owned by the new context-aware forecast chain."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)

from investment_manager.platform.database import metadata

codex_runs = Table(
    "codex_runs",
    metadata,
    Column("run_id", String(128), primary_key=True),
    Column("cycle_id", String(128), nullable=False),
    Column("account_id", String(64), nullable=True),
    Column("attempt", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("error_class", String(64), nullable=True),
    Column("payload", JSON, nullable=False),
)
Index("ix_codex_runs_cycle_status", codex_runs.c.cycle_id, codex_runs.c.status)

codex_account_capacity = Table(
    "codex_account_capacity",
    metadata,
    Column("account_id", String(64), primary_key=True),
    Column("observed_at", DateTime(timezone=True), primary_key=True),
    Column("effective_headroom", Numeric(8, 3), nullable=True),
    Column("healthy", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
)

codex_account_leases = Table(
    "codex_account_leases",
    metadata,
    Column("lease_id", String(128), primary_key=True),
    Column("account_id", String(64), nullable=False),
    Column("cycle_id", String(128), nullable=False),
    Column("attempt_id", String(128), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
)
Index(
    "uq_active_codex_account_lease",
    codex_account_leases.c.account_id,
    unique=True,
    postgresql_where=codex_account_leases.c.status == "ACTIVE",
    sqlite_where=codex_account_leases.c.status == "ACTIVE",
)

context_assessments = Table(
    "context_assessments",
    metadata,
    Column("assessment_id", String(128), primary_key=True),
    Column("packet_id", ForeignKey("decision_packets.packet_id"), nullable=False),
    Column("analysis_scope", String(128), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=False),
    Column("view_count", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "packet_id",
        "analysis_behavior_hash",
        name="uq_context_assessment_packet_behavior",
    ),
)
Index(
    "ix_context_assessments_behavior_available",
    context_assessments.c.analysis_behavior_hash,
    context_assessments.c.available_at,
)

assessment_view_outcomes = Table(
    "assessment_view_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column(
        "assessment_id",
        ForeignKey("context_assessments.assessment_id"),
        nullable=False,
    ),
    Column("analysis_behavior_hash", String(64), nullable=False),
    Column("asset", String(64), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("horizon_minutes", Integer, nullable=False),
    Column("direction", String(32), nullable=False),
    Column("already_priced", String(32), nullable=False),
    Column("uncertainty", String(32), nullable=False),
    Column("evaluation_version", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("signal_observed_at", DateTime(timezone=True), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=False),
    Column("directional_return_bps", Numeric(38, 18), nullable=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "assessment_id",
        "asset",
        "horizon_minutes",
        "evaluation_version",
        name="uq_assessment_view_outcome_identity",
    ),
)
Index(
    "ix_assessment_view_outcomes_cohort",
    assessment_view_outcomes.c.analysis_behavior_hash,
    assessment_view_outcomes.c.asset,
    assessment_view_outcomes.c.symbol,
    assessment_view_outcomes.c.horizon_minutes,
    assessment_view_outcomes.c.evaluation_at,
)
