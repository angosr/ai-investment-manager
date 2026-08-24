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

assessment_executions = Table(
    "assessment_executions",
    metadata,
    Column("execution_id", String(128), primary_key=True),
    Column("packet_id", ForeignKey("decision_packets.packet_id"), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("source_run_id", String(128), nullable=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_assessment_executions_behavior_completed",
    assessment_executions.c.analysis_behavior_hash,
    assessment_executions.c.completed_at,
)

context_mechanism_observations = Table(
    "context_mechanism_observations",
    metadata,
    Column("observation_id", String(128), primary_key=True),
    Column(
        "assessment_id",
        ForeignKey("context_assessments.assessment_id"),
        nullable=False,
    ),
    Column("mechanism_id", String(128), nullable=False),
    Column("test_id", String(128), nullable=False),
    Column(
        "packet_id",
        ForeignKey("decision_packets.packet_id"),
        nullable=False,
    ),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("resolution", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "assessment_id",
        "test_id",
        "packet_id",
        name="uq_context_mechanism_observation",
    ),
)
Index(
    "ix_context_mechanism_observations_test_time",
    context_mechanism_observations.c.assessment_id,
    context_mechanism_observations.c.test_id,
    context_mechanism_observations.c.observed_at,
)

forecast_contracts = Table(
    "forecast_contracts",
    metadata,
    Column("contract_id", String(128), primary_key=True),
    Column("contract_version", String(128), nullable=False),
    Column("outcome_family_id", String(128), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("horizon_minutes", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_forecast_contracts_family_version",
    forecast_contracts.c.outcome_family_id,
    forecast_contracts.c.contract_version,
)

forecast_producer_bindings = Table(
    "forecast_producer_bindings",
    metadata,
    Column("binding_id", String(128), primary_key=True),
    Column(
        "contract_id",
        ForeignKey("forecast_contracts.contract_id"),
        nullable=False,
    ),
    Column("producer_kind", String(32), nullable=False),
    Column("producer_id", String(128), nullable=False),
    Column("producer_behavior_id", String(128), nullable=False),
    Column("permission", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "contract_id",
        "producer_behavior_id",
        name="uq_forecast_binding_contract_behavior",
    ),
)

forecast_decision_slots = Table(
    "forecast_decision_slots",
    metadata,
    Column("slot_id", String(128), primary_key=True),
    Column(
        "contract_id",
        ForeignKey("forecast_contracts.contract_id"),
        nullable=False,
    ),
    Column("slot_as_of", DateTime(timezone=True), nullable=False),
    Column("information_cutoff_at", DateTime(timezone=True), nullable=False),
    Column("completion_deadline_at", DateTime(timezone=True), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "contract_id",
        "slot_as_of",
        name="uq_forecast_decision_slot_contract_time",
    ),
)
Index(
    "ix_forecast_decision_slots_evaluation",
    forecast_decision_slots.c.evaluation_at,
    forecast_decision_slots.c.slot_id,
)

forecast_slot_obligations = Table(
    "forecast_slot_obligations",
    metadata,
    Column("obligation_id", String(128), primary_key=True),
    Column(
        "slot_id",
        ForeignKey("forecast_decision_slots.slot_id"),
        nullable=False,
    ),
    Column(
        "contract_id",
        ForeignKey("forecast_contracts.contract_id"),
        nullable=False,
    ),
    Column(
        "binding_id",
        ForeignKey("forecast_producer_bindings.binding_id"),
        nullable=False,
    ),
    Column("producer_kind", String(32), nullable=False),
    Column("producer_id", String(128), nullable=False),
    Column("producer_behavior_id", String(128), nullable=False),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "slot_id",
        "producer_behavior_id",
        name="uq_forecast_slot_obligation_behavior",
    ),
)
Index(
    "ix_forecast_slot_obligations_behavior_time",
    forecast_slot_obligations.c.producer_behavior_id,
    forecast_slot_obligations.c.assigned_at,
)

forecast_no_estimates = Table(
    "forecast_no_estimates",
    metadata,
    Column("result_id", String(128), primary_key=True),
    Column(
        "slot_id",
        ForeignKey("forecast_decision_slots.slot_id"),
        nullable=False,
    ),
    Column(
        "contract_id",
        ForeignKey("forecast_contracts.contract_id"),
        nullable=False,
    ),
    Column("producer_kind", String(32), nullable=False),
    Column("producer_id", String(128), nullable=False),
    Column("producer_behavior_id", String(128), nullable=False),
    Column("reason", String(64), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "slot_id",
        "producer_behavior_id",
        name="uq_forecast_no_estimate_slot_behavior",
    ),
)
Index(
    "ix_forecast_no_estimates_behavior_time",
    forecast_no_estimates.c.producer_behavior_id,
    forecast_no_estimates.c.completed_at,
)

historical_opportunity_reviews = Table(
    "historical_opportunity_reviews",
    metadata,
    Column("review_id", String(128), primary_key=True),
    Column("opportunity_id", String(128), nullable=False),
    Column("world_model_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
historical_opportunity_assessments = Table(
    "historical_opportunity_assessments",
    metadata,
    Column("assessment_id", String(128), primary_key=True),
    Column(
        "review_id",
        String(128),
        nullable=False,
    ),
    Column("opportunity_id", String(128), nullable=False),
    Column("world_model_id", String(128), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

historical_assessment_view_outcomes = Table(
    "historical_assessment_view_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column("assessment_id", String(128), nullable=False),
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
)

historical_context_assessments = Table(
    "historical_context_assessments",
    metadata,
    Column("assessment_id", String(128), primary_key=True),
    Column("packet_id", String(128), nullable=False),
    Column("analysis_scope", String(128), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_historical_assessment_view_outcomes_cohort",
    historical_assessment_view_outcomes.c.analysis_behavior_hash,
    historical_assessment_view_outcomes.c.asset,
    historical_assessment_view_outcomes.c.symbol,
    historical_assessment_view_outcomes.c.horizon_minutes,
    historical_assessment_view_outcomes.c.evaluation_at,
)

# Immutable facts produced by the retired score/direction forecast runtime.  They
# remain queryable for audit, but no active repository writes or reads them.
historical_forecasts = Table(
    "historical_forecasts",
    metadata,
    Column("forecast_id", String(128), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("producer_id", String(128), nullable=False),
    Column("producer_version", String(128), nullable=False),
    Column("forecast_family", String(128), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("base_forecast_id", String(128), nullable=True),
    Column("assessment_id", String(128), nullable=True),
    Column("payload", JSON, nullable=False),
)

historical_forecast_outcomes = Table(
    "historical_forecast_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column("forecast_id", String(128), nullable=False),
    Column("evaluation_version", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=False),
    Column("gross_target_return_bps", Numeric(38, 18), nullable=True),
    Column("payload", JSON, nullable=False),
)

forecasts = Table(
    "forecasts",
    metadata,
    Column("forecast_id", String(128), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column(
        "contract_id",
        ForeignKey("forecast_contracts.contract_id"),
        nullable=False,
    ),
    Column(
        "decision_slot_id",
        ForeignKey("forecast_decision_slots.slot_id"),
        nullable=False,
    ),
    Column("producer_id", String(128), nullable=False),
    Column("producer_behavior_id", String(128), nullable=False),
    Column("outcome_family_id", String(128), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("base_forecast_id", ForeignKey("forecasts.forecast_id"), nullable=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "decision_slot_id",
        "producer_behavior_id",
        "kind",
        name="uq_forecast_slot_behavior_kind",
    ),
)
Index(
    "ix_forecasts_contract_slot",
    forecasts.c.contract_id,
    forecasts.c.decision_slot_id,
)
Index(
    "ix_forecasts_producer_target",
    forecasts.c.producer_id,
    forecasts.c.producer_behavior_id,
    forecasts.c.target_id,
    forecasts.c.available_at,
)

forecast_outcomes = Table(
    "forecast_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column(
        "contract_id",
        ForeignKey("forecast_contracts.contract_id"),
        nullable=False,
    ),
    Column(
        "decision_slot_id",
        ForeignKey("forecast_decision_slots.slot_id"),
        nullable=False,
    ),
    Column("evaluation_version", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=False),
    Column("gross_target_return_bps", Numeric(38, 18), nullable=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "decision_slot_id",
        "evaluation_version",
        name="uq_forecast_outcome_identity",
    ),
)
Index(
    "ix_forecast_outcomes_cohort",
    forecast_outcomes.c.evaluation_version,
    forecast_outcomes.c.evaluation_at,
    forecast_outcomes.c.decision_slot_id,
)
