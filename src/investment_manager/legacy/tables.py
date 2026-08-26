"""Tables owned exclusively by the retired single-symbol decision chain.

These tables remain readable for historical research and migration verification.  They are
deliberately absent from the managed runtime schema assembled by ``investment_manager.schema``.
"""

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
    Text,
    UniqueConstraint,
)

from investment_manager.platform.database import metadata

analysis_cycles = Table(
    "analysis_cycles",
    metadata,
    Column("cycle_id", String(128), primary_key=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("pipeline_version", String(128), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("reason_code", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_analysis_cycles_as_of", analysis_cycles.c.as_of)
Index(
    "ix_analysis_cycles_pipeline_as_of",
    analysis_cycles.c.pipeline_version,
    analysis_cycles.c.as_of,
)

market_snapshots = Table(
    "market_snapshots",
    metadata,
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_market_snapshots_symbol_as_of", market_snapshots.c.symbol, market_snapshots.c.as_of)

panel_snapshots = Table(
    "panel_snapshots",
    metadata,
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), primary_key=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index("ix_panel_snapshots_as_of", panel_snapshots.c.as_of)

analysis_proposals = Table(
    "analysis_proposals",
    metadata,
    Column("proposal_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("proposal_type", String(32), nullable=False),
    Column("suggested_action", String(32), nullable=False),
    Column("forecast_count", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

signal_candidates = Table(
    "signal_candidates",
    metadata,
    Column("candidate_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("producer_id", String(128), nullable=False),
    Column("producer_version", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("cycle_id", "sequence", name="uq_signal_candidate_cycle_sequence"),
)
Index("ix_signal_candidates_cycle", signal_candidates.c.cycle_id)

trade_intents = Table(
    "trade_intents",
    metadata,
    Column("intent_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("pipeline_version", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

risk_decisions = Table(
    "risk_decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("intent_id", ForeignKey("trade_intents.intent_id"), nullable=False, unique=True),
    Column("outcome", String(32), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
)

account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("reconciled", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("cycle_id", "phase", name="uq_account_snapshot_cycle_phase"),
)
Index("ix_account_snapshots_as_of", account_snapshots.c.as_of)

execution_requests = Table(
    "execution_requests",
    metadata,
    Column("execution_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("result_payload", JSON, nullable=True),
)
Index("ix_execution_requests_status", execution_requests.c.status)

mock_exchange_orders = Table(
    "mock_exchange_orders",
    metadata,
    Column("client_order_id", String(36), primary_key=True),
    Column("order_id", String(128), nullable=False, unique=True),
    Column("role", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_mock_exchange_orders_observed_at", mock_exchange_orders.c.observed_at)

mock_exchange_protections = Table(
    "mock_exchange_protections",
    metadata,
    Column("protection_id", String(128), primary_key=True),
    Column("position_id", String(128), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

reconciliation_reports = Table(
    "reconciliation_reports",
    metadata,
    Column("report_id", String(128), primary_key=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("freeze_new_risk", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_reconciliation_reports_as_of", reconciliation_reports.c.as_of)
Index("ix_reconciliation_reports_status", reconciliation_reports.c.status)

orders = Table(
    "orders",
    metadata,
    Column("order_id", String(128), primary_key=True),
    Column("client_order_id", String(36), nullable=False, unique=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("intent_id", ForeignKey("trade_intents.intent_id"), nullable=False),
    Column("role", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("cycle_id", "role", name="uq_order_cycle_role"),
)
Index("ix_orders_role_cycle", orders.c.role, orders.c.cycle_id)

fills = Table(
    "fills",
    metadata,
    Column("fill_id", String(128), primary_key=True),
    Column("order_id", ForeignKey("orders.order_id"), nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_fills_order", fills.c.order_id)

position_lifecycles = Table(
    "position_lifecycles",
    metadata,
    Column("position_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("intent_id", ForeignKey("trade_intents.intent_id"), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

decision_outcomes = Table(
    "decision_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("intent_id", ForeignKey("trade_intents.intent_id"), nullable=False, unique=True),
    Column("position_id", ForeignKey("position_lifecycles.position_id"), nullable=False),
    Column("net_pnl", Numeric(38, 18), nullable=False),
    Column("payload", JSON, nullable=False),
)

candidate_outcomes = Table(
    "candidate_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column(
        "candidate_id",
        ForeignKey("signal_candidates.candidate_id"),
        nullable=False,
        unique=True,
    ),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("status", String(32), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=False),
    Column("net_return_bps", Numeric(38, 18), nullable=True),
    Column("payload", JSON, nullable=False),
)
Index("ix_candidate_outcomes_evaluation_at", candidate_outcomes.c.evaluation_at)

analysis_forecast_outcomes = Table(
    "analysis_forecast_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column("proposal_id", ForeignKey("analysis_proposals.proposal_id"), nullable=False),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("pipeline_version", String(128), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=True),
    Column("view_horizon_minutes", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=False),
    Column("directional_return_bps", Numeric(38, 18), nullable=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "proposal_id",
        "view_horizon_minutes",
        name="uq_analysis_forecast_outcomes_proposal_horizon",
    ),
)
Index(
    "ix_analysis_forecast_outcomes_pipeline_evaluation",
    analysis_forecast_outcomes.c.pipeline_version,
    analysis_forecast_outcomes.c.evaluation_at,
)
Index(
    "ix_analysis_forecast_outcomes_behavior_evaluation",
    analysis_forecast_outcomes.c.analysis_behavior_hash,
    analysis_forecast_outcomes.c.evaluation_at,
)

metric_observations = Table(
    "metric_observations",
    metadata,
    Column("metric_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("metric_version", String(128), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("cycle_id", "phase", "sequence", name="uq_metric_cycle_phase_sequence"),
)
Index("ix_metric_observations_cycle", metric_observations.c.cycle_id)

outcome_window_reports = Table(
    "outcome_window_reports",
    metadata,
    Column("report_id", String(128), primary_key=True),
    Column("evaluation_version", String(128), nullable=False),
    Column("pipeline_version", String(128), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("source_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_outcome_window_reports_window",
    outcome_window_reports.c.pipeline_version,
    outcome_window_reports.c.window_start,
    outcome_window_reports.c.window_end,
)

architecture_decisions = Table(
    "architecture_decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("summary", Text, nullable=False),
    Column("payload", JSON, nullable=False),
)

replay_evaluation_reports = Table(
    "replay_evaluation_reports",
    metadata,
    Column("report_id", String(128), primary_key=True),
    Column("evaluation_version", String(128), nullable=False),
    Column("dataset_hash", String(64), nullable=False),
    Column("statistically_conclusive", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
)

# Retired automatic-governance facts.  Existing rows remain queryable through the
# offline schema, but no managed worker is allowed to create new ones.
system_constitutions = Table(
    "system_constitutions",
    metadata,
    Column("version", String(128), primary_key=True),
    Column("payload", JSON, nullable=False),
)

governance_snapshots = Table(
    "governance_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("champion_manifest_id", String(128), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)

governance_decisions = Table(
    "governance_decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column(
        "snapshot_id",
        ForeignKey("governance_snapshots.snapshot_id"),
        nullable=False,
        unique=True,
    ),
    Column("decision_type", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

change_proposals = Table(
    "change_proposals",
    metadata,
    Column("proposal_id", String(128), primary_key=True),
    Column("base_version", String(128), nullable=False),
    Column("change_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

evaluation_results = Table(
    "evaluation_results",
    metadata,
    Column("evaluation_id", String(128), primary_key=True),
    Column("proposal_id", String(128), nullable=False),
    Column("plan_id", String(128), nullable=False),
    Column("candidate_manifest_id", String(128), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

release_approval_requests = Table(
    "release_approval_requests",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column(
        "evaluation_id",
        ForeignKey("evaluation_results.evaluation_id"),
        nullable=False,
        unique=True,
    ),
    Column("candidate_manifest_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
