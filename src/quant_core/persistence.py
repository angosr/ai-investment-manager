from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    case,
    create_engine,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from quant_core.analyst import AttemptAudit, CapacitySnapshot, CodexLease
from quant_core.domain import (
    AccountSnapshot,
    AnalysisProposal,
    DecisionOutcome,
    IntelligenceEvent,
    MetricObservation,
    Order,
    PanelSnapshot,
    PositionLifecycle,
    RiskDecision,
    SignalCandidate,
    TradeIntent,
    _require_utc,
)
from quant_core.evaluation import ReplayEvaluationReport
from quant_core.execution_contract import (
    ExecutionRequest,
    ExecutionResult,
    RiskTransition,
)
from quant_core.governance import (
    BlindEvaluationClaim,
    ChangeProposal,
    EvaluationPlan,
    EvaluationResult,
    EvaluationStage,
    EvaluationTarget,
    FailedExperiment,
    GovernanceSnapshot,
    ReleaseApprovalDecision,
    ReleaseManifest,
    SystemConstitution,
)
from quant_core.ids import content_hash, stable_id
from quant_core.ledger import CycleFacts, LifecycleFacts, RiskReservationRejected
from quant_core.lifecycle import OpenLifecycleRecord
from quant_core.risk_budget import ReservationClaim
from quant_core.trigger import (
    AnalysisTriggerEvent,
    AnalysisTriggerType,
    TriggerOutboxKind,
    build_trigger_event,
)

metadata = MetaData()
DATABASE_SCHEMA_VERSION = "b3f6e1a8c920"


def notify_trigger_outbox(connection: Connection, aggregate_key: str) -> None:
    """PostgreSQL NOTIFY 只负责低延迟唤醒；Outbox 行仍是可靠事实。"""

    if connection.dialect.name == "postgresql":
        connection.execute(select(func.pg_notify("quant_trigger_outbox", aggregate_key)))


def insert_trigger_with_outbox(
    connection: Connection,
    trigger: AnalysisTriggerEvent,
) -> None:
    """所有触发来源共享同一事务写路径和幂等身份。"""

    trigger_payload = trigger.model_dump(mode="json")
    connection.execute(
        insert(analysis_trigger_events).values(
            trigger_id=trigger.trigger_id,
            trigger_type=trigger.trigger_type.value,
            symbol=trigger.symbol,
            pipeline_id=trigger.pipeline_id,
            occurred_at=trigger.occurred_at,
            observed_at=trigger.observed_at,
            priority=trigger.priority,
            dedup_key=trigger.dedup_key,
            expires_at=trigger.expires_at,
            payload=trigger_payload,
        )
    )
    outbox_id = stable_id(
        "trigger_outbox",
        TriggerOutboxKind.TRIGGER_CREATED.value,
        trigger.trigger_id,
    )
    connection.execute(
        insert(trigger_outbox).values(
            outbox_id=outbox_id,
            aggregate_key=f"{trigger.symbol}:{trigger.pipeline_id}",
            message_kind=TriggerOutboxKind.TRIGGER_CREATED.value,
            status="PENDING",
            created_at=trigger.observed_at,
            available_at=trigger.observed_at,
            delivered_at=None,
            attempt_count=0,
            payload={
                "kind": TriggerOutboxKind.TRIGGER_CREATED.value,
                "trigger": trigger_payload,
            },
        )
    )
    notify_trigger_outbox(connection, f"{trigger.symbol}:{trigger.pipeline_id}")


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

risk_reservations = Table(
    "risk_reservations",
    metadata,
    Column("reservation_id", String(128), primary_key=True),
    Column("cycle_id", String(128), nullable=False, unique=True),
    Column("intent_id", String(128), nullable=False, unique=True),
    Column("symbol", String(32), nullable=False),
    Column("risk_amount", Numeric(38, 18), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

portfolio_risk_budgets = Table(
    "portfolio_risk_budgets",
    metadata,
    Column("portfolio_id", String(64), primary_key=True),
    Column("reserved_amount", Numeric(38, 18), nullable=False),
    Column("exposure_risk_amount", Numeric(38, 18), nullable=False),
)

portfolio_protection_states = Table(
    "portfolio_protection_states",
    metadata,
    Column("portfolio_id", String(64), primary_key=True),
    Column("kill_switch_active", Boolean, nullable=False),
    Column("high_water_equity", Numeric(38, 18), nullable=True),
    Column("last_equity", Numeric(38, 18), nullable=True),
    Column("drawdown_fraction", Numeric(38, 18), nullable=False),
    Column("trip_reason", String(128), nullable=True),
    Column("tripped_at", DateTime(timezone=True), nullable=True),
    Column("last_reset_at", DateTime(timezone=True), nullable=True),
    Column("last_reset_reason", String(512), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True),
)

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
    Column(
        "proposal_id",
        ForeignKey("analysis_proposals.proposal_id"),
        nullable=False,
    ),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("pipeline_version", String(128), nullable=False),
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

normalized_events = Table(
    "normalized_events",
    metadata,
    Column("evidence_id", String(128), primary_key=True),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("source", String(128), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("source", "content_hash", name="uq_normalized_event_source_hash"),
)
# Dashboard 世界事件按 event_time 倒序分页；交易热路径通过 trigger scope 关联后有界读取。
Index("ix_normalized_events_event_time", normalized_events.c.event_time)

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
# plan_for_scope 每次投递都按 (symbol, pipeline_id, is_current) 取当前计划，是热路径；
# 部分索引只覆盖当前行，避免随修订历史增长而退化为顺序扫描。
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

release_manifests = Table(
    "release_manifests",
    metadata,
    Column("manifest_id", String(128), primary_key=True),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

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

evaluation_plans = Table(
    "evaluation_plans",
    metadata,
    Column("plan_id", String(128), primary_key=True),
    Column("registered_at", DateTime(timezone=True), nullable=False),
    Column("base_manifest_id", String(128), nullable=False),
    Column("regression_suite_version", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
)

blind_evaluation_claims = Table(
    "blind_evaluation_claims",
    metadata,
    Column(
        "plan_id",
        ForeignKey("evaluation_plans.plan_id"),
        primary_key=True,
    ),
    Column("query_id", String(128), nullable=False, unique=True),
    Column("source_evaluation_id", String(128), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("result_id", String(128), nullable=True),
    Column("result_hash", String(64), nullable=True),
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

failed_experiment_records = Table(
    "failed_experiments",
    metadata,
    Column("experiment_id", String(128), primary_key=True),
    Column("hypothesis_fingerprint", String(64), nullable=False),
    Column("rejected_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_failed_experiment_fingerprint", failed_experiment_records.c.hypothesis_fingerprint)

replay_evaluation_reports = Table(
    "replay_evaluation_reports",
    metadata,
    Column("report_id", String(128), primary_key=True),
    Column("evaluation_version", String(128), nullable=False),
    Column("dataset_hash", String(64), nullable=False),
    Column("statistically_conclusive", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
)

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

change_proposals = Table(
    "change_proposals",
    metadata,
    Column("proposal_id", String(128), primary_key=True),
    Column("base_version", String(128), nullable=False),
    Column("change_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

architecture_decisions = Table(
    "architecture_decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("summary", Text, nullable=False),
    Column("payload", JSON, nullable=False),
)


def latest_account_snapshot_payload(connection: Connection, *, as_of: datetime):
    """给定 ``as_of``，返回"当前账户"快照 payload（无则 None）。

    规则：按 ``as_of`` 倒序，同一 ``as_of`` 内按 phase 优先 POST_EXIT > POST_EXECUTION >
    其它。影子账户投影（``shadow.py``）与对账本地态（``reconciliation_sql.py``）必须共用
    这一条可见性规则，否则"两处看到的当前账户"会静默分裂。
    """

    phase_priority = case(
        (account_snapshots.c.phase == "POST_EXIT", 3),
        (account_snapshots.c.phase == "POST_EXECUTION", 2),
        else_=1,
    )
    return connection.execute(
        select(account_snapshots.c.payload)
        .where(account_snapshots.c.as_of <= as_of)
        .order_by(
            account_snapshots.c.as_of.desc(),
            phase_priority.desc(),
            account_snapshots.c.snapshot_id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    return create_engine(database_url, echo=echo, future=True)


def require_current_schema(engine: Engine) -> None:
    """Fail closed before a runtime service touches an absent or stale schema."""

    try:
        with engine.connect() as connection:
            versions = tuple(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
    except SQLAlchemyError as exc:
        raise RuntimeError("数据库 Schema 版本不可读；拒绝启动运行服务") from exc
    if versions != (DATABASE_SCHEMA_VERSION,):
        observed = versions[0] if len(versions) == 1 else "MISSING_OR_MULTIPLE_HEADS"
        raise RuntimeError(
            f"数据库 Schema 版本不匹配：expected={DATABASE_SCHEMA_VERSION}, observed={observed}"
        )


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as connection:
        exists = connection.execute(
            select(portfolio_risk_budgets.c.portfolio_id).where(
                portfolio_risk_budgets.c.portfolio_id == "primary"
            )
        ).scalar_one_or_none()
        if exists is None:
            connection.execute(
                insert(portfolio_risk_budgets).values(
                    portfolio_id="primary",
                    reserved_amount=0,
                    exposure_risk_amount=0,
                )
            )
        protection_exists = connection.execute(
            select(portfolio_protection_states.c.portfolio_id).where(
                portfolio_protection_states.c.portfolio_id == "primary"
            )
        ).scalar_one_or_none()
        if protection_exists is None:
            connection.execute(
                insert(portfolio_protection_states).values(
                    portfolio_id="primary",
                    kill_switch_active=False,
                    high_water_equity=None,
                    last_equity=None,
                    drawdown_fraction=0,
                    trip_reason=None,
                    tripped_at=None,
                    last_reset_at=None,
                    last_reset_reason=None,
                    updated_at=None,
                )
            )


class SqlRiskBudgetStore:
    """通过组合预算行锁实现跨 Worker 的原子风险占用。"""

    def __init__(self, engine: Engine, *, portfolio_id: str = "primary") -> None:
        self._engine = engine
        self._portfolio_id = portfolio_id

    def reserve(self, reservation, *, maximum_total_risk) -> ReservationClaim:
        try:
            with self._engine.begin() as connection:
                budget = (
                    connection.execute(
                        select(portfolio_risk_budgets)
                        .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                existing = connection.execute(
                    select(risk_reservations.c.status).where(
                        risk_reservations.c.reservation_id == reservation.reservation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return ReservationClaim(False, "RESERVATION_ALREADY_EXISTS")
                committed = budget["reserved_amount"] + budget["exposure_risk_amount"]
                if committed + reservation.risk_amount > maximum_total_risk:
                    return ReservationClaim(False, "PORTFOLIO_RISK_BUDGET_EXHAUSTED")
                connection.execute(
                    insert(risk_reservations).values(
                        reservation_id=reservation.reservation_id,
                        cycle_id=reservation.cycle_id,
                        intent_id=reservation.intent_id,
                        symbol=reservation.symbol,
                        risk_amount=reservation.risk_amount,
                        expires_at=reservation.expires_at,
                        status="ACTIVE",
                        payload=reservation.model_dump(mode="json"),
                    )
                )
                connection.execute(
                    update(portfolio_risk_budgets)
                    .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                    .values(reserved_amount=budget["reserved_amount"] + reservation.risk_amount)
                )
        except IntegrityError:
            return ReservationClaim(False, "RESERVATION_ALREADY_EXISTS")
        return ReservationClaim(True, "RISK_RESERVED")

    def consume(self, reservation_id: str) -> None:
        with self._engine.begin() as connection:
            budget = self._locked_budget(connection)
            reservation = (
                connection.execute(
                    select(risk_reservations)
                    .where(risk_reservations.c.reservation_id == reservation_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if reservation["status"] != "ACTIVE":
                raise ValueError(f"Reservation 状态不能转为 CONSUMED: {reservation['status']}")
            amount = reservation["risk_amount"]
            connection.execute(
                update(risk_reservations)
                .where(risk_reservations.c.reservation_id == reservation_id)
                .values(status="CONSUMED")
            )
            connection.execute(
                update(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                .values(
                    reserved_amount=budget["reserved_amount"] - amount,
                    exposure_risk_amount=budget["exposure_risk_amount"] + amount,
                )
            )

    def release(self, reservation_id: str) -> None:
        with self._engine.begin() as connection:
            budget = self._locked_budget(connection)
            reservation = (
                connection.execute(
                    select(risk_reservations)
                    .where(risk_reservations.c.reservation_id == reservation_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            status = reservation["status"]
            if status == "RELEASED":
                return
            amount = reservation["risk_amount"]
            if status == "ACTIVE":
                budget_values = {"reserved_amount": budget["reserved_amount"] - amount}
            elif status == "CONSUMED":
                budget_values = {"exposure_risk_amount": budget["exposure_risk_amount"] - amount}
            else:
                raise ValueError(f"未知 Reservation 状态: {status}")
            connection.execute(
                update(risk_reservations)
                .where(risk_reservations.c.reservation_id == reservation_id)
                .values(status="RELEASED")
            )
            connection.execute(
                update(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                .values(**budget_values)
            )

    def _locked_budget(self, connection: Connection):
        return (
            connection.execute(
                select(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )


class SqlAccountLeaseStore:
    """数据库唯一约束保证跨 Worker 每账号最多一个 ACTIVE 租约。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def try_acquire(
        self, account_id: str, cycle_id: str, attempt_id: str, expires_at
    ) -> CodexLease | None:
        lease = CodexLease(
            lease_id=stable_id("lease", account_id, cycle_id, attempt_id),
            account_id=account_id,
            cycle_id=cycle_id,
            attempt_id=attempt_id,
            expires_at=expires_at,
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    update(codex_account_leases)
                    .where(
                        codex_account_leases.c.account_id == account_id,
                        codex_account_leases.c.status == "ACTIVE",
                        codex_account_leases.c.expires_at <= datetime.now(tz=UTC),
                    )
                    .values(status="EXPIRED")
                )
                connection.execute(
                    insert(codex_account_leases).values(
                        lease_id=lease.lease_id,
                        account_id=account_id,
                        cycle_id=cycle_id,
                        attempt_id=attempt_id,
                        expires_at=expires_at,
                        status="ACTIVE",
                    )
                )
        except IntegrityError:
            return None
        return lease

    def release(self, lease_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(codex_account_leases)
                .where(
                    codex_account_leases.c.lease_id == lease_id,
                    codex_account_leases.c.status == "ACTIVE",
                )
                .values(status="RELEASED")
            )

    def has_active(self, account_id: str, now) -> bool:
        with self._engine.begin() as connection:
            connection.execute(
                update(codex_account_leases)
                .where(
                    codex_account_leases.c.account_id == account_id,
                    codex_account_leases.c.status == "ACTIVE",
                    codex_account_leases.c.expires_at <= now,
                )
                .values(status="EXPIRED")
            )
            active = connection.execute(
                select(codex_account_leases.c.lease_id).where(
                    codex_account_leases.c.account_id == account_id,
                    codex_account_leases.c.status == "ACTIVE",
                )
            ).scalar_one_or_none()
        return active is not None


class SqlCodexAuditStore:
    """仅保存匿名账号、额度窗口和运行元数据，不保存目录或完整账号响应。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_capacity(self, snapshot: CapacitySnapshot) -> None:
        payload = {
            "account_id": snapshot.account_id,
            "observed_at": snapshot.observed_at.isoformat(),
            "effective_headroom": str(snapshot.effective_headroom),
            "buckets": [
                {
                    "limit_id": bucket.limit_id,
                    "primary": self._window_payload(bucket.primary),
                    "secondary": self._window_payload(bucket.secondary),
                    "reached_type": bucket.reached_type,
                }
                for bucket in snapshot.buckets
            ],
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(codex_account_capacity).values(
                        account_id=snapshot.account_id,
                        observed_at=snapshot.observed_at,
                        effective_headroom=snapshot.effective_headroom,
                        healthy=snapshot.effective_headroom > 0,
                        payload=payload,
                    )
                )
        except IntegrityError:
            return

    def record_attempt(self, attempt: AttemptAudit) -> None:
        payload = {
            "observed_at": attempt.observed_at.isoformat(),
            "completed_at": attempt.completed_at.isoformat(),
            "duration_ms": attempt.duration_ms,
            "runtime_policy_version": attempt.runtime_policy_version,
            "bundle_hash": attempt.bundle_hash,
            "usage": attempt.usage,
            "diagnostics": attempt.diagnostics,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(codex_runs).values(
                        run_id=attempt.run_id,
                        cycle_id=attempt.cycle_id,
                        account_id=attempt.account_id,
                        attempt=attempt.attempt,
                        status=attempt.status,
                        error_class=attempt.failure.value if attempt.failure else None,
                        payload=payload,
                    )
                )
        except IntegrityError:
            return

    @staticmethod
    def _window_payload(window):
        if window is None:
            return None
        return {
            "used_percent": str(window.used_percent),
            "window_duration_minutes": window.window_duration_minutes,
            "resets_at": window.resets_at.isoformat(),
        }


class SqlGovernanceRepository:
    """主 Agent 的长期记忆只由这些版本化事实构成，不保存聊天上下文。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_constitution(self, constitution: SystemConstitution) -> None:
        self._record(
            system_constitutions,
            system_constitutions.c.version,
            constitution.version,
            {
                "version": constitution.version,
                "payload": constitution.model_dump(mode="json"),
            },
        )

    def record_release(self, release: ReleaseManifest) -> None:
        self._record(
            release_manifests,
            release_manifests.c.manifest_id,
            release.manifest_id,
            {
                "manifest_id": release.manifest_id,
                "content_hash": content_hash(release),
                "status": release.status,
                "payload": release.model_dump(mode="json"),
            },
        )

    def get_champion(self) -> ReleaseManifest:
        """Return the single release authorized as Champion, or fail closed."""

        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(release_manifests.c.payload).where(
                        release_manifests.c.status == "CHAMPION"
                    )
                ).scalars()
            )
        if len(payloads) != 1:
            raise ValueError("治理事实库中必须恰好存在一个 CHAMPION")
        return ReleaseManifest.model_validate(payloads[0])

    def record_snapshot(self, snapshot: GovernanceSnapshot) -> None:
        self._record(
            governance_snapshots,
            governance_snapshots.c.snapshot_id,
            snapshot.snapshot_id,
            {
                "snapshot_id": snapshot.snapshot_id,
                "as_of": snapshot.as_of,
                "champion_manifest_id": snapshot.champion.manifest_id,
                "content_hash": snapshot.content_hash,
                "payload": snapshot.model_dump(mode="json"),
            },
        )

    def get_snapshot(self, snapshot_id: str) -> GovernanceSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(governance_snapshots.c.payload).where(
                    governance_snapshots.c.snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
        return GovernanceSnapshot.model_validate(payload) if payload else None

    def register_plan(self, plan: EvaluationPlan) -> None:
        self._record(
            evaluation_plans,
            evaluation_plans.c.plan_id,
            plan.plan_id,
            {
                "plan_id": plan.plan_id,
                "registered_at": plan.registered_at,
                "base_manifest_id": plan.base_manifest_id,
                "regression_suite_version": plan.fixed_regression_suite_version,
                "payload": plan.model_dump(mode="json"),
            },
        )

    def get_plan(self, plan_id: str) -> EvaluationPlan | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(evaluation_plans.c.payload).where(
                    evaluation_plans.c.plan_id == plan_id
                )
            ).scalar_one_or_none()
        return EvaluationPlan.model_validate(payload) if payload else None

    def claim_blind_evaluation(self, claim: BlindEvaluationClaim) -> BlindEvaluationClaim:
        """Atomically consume one plan's blind query budget; exact retries resume."""

        if any(
            item is not None
            for item in (claim.completed_at, claim.result_id, claim.result_hash)
        ):
            raise ValueError("首次认领盲测预算时不能携带完成结果")
        plan = self.get_plan(claim.plan_id)
        if (
            plan is None
            or EvaluationStage.BLIND not in plan.required_stages
            or plan.blind_query_budget != 1
        ):
            raise ValueError("EvaluationPlan 没有可消费的一次性盲测预算")
        values = {
            "plan_id": claim.plan_id,
            "query_id": claim.query_id,
            "source_evaluation_id": claim.source_evaluation_id,
            "claimed_at": claim.claimed_at,
            "payload": claim.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(blind_evaluation_claims).values(**values))
            return claim
        except IntegrityError:
            existing = self.get_blind_evaluation_claim(claim.plan_id)
            if existing is None or (
                existing.query_id != claim.query_id
                or existing.source_evaluation_id != claim.source_evaluation_id
            ):
                raise ValueError("EvaluationPlan 的盲测查询预算已经被消费") from None
            return existing

    def get_blind_evaluation_claim(self, plan_id: str) -> BlindEvaluationClaim | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(blind_evaluation_claims.c.payload).where(
                    blind_evaluation_claims.c.plan_id == plan_id
                )
            ).scalar_one_or_none()
        return BlindEvaluationClaim.model_validate(payload) if payload else None

    def complete_blind_evaluation(self, completed: BlindEvaluationClaim) -> BlindEvaluationClaim:
        """Complete a claimed reveal once; the identical completion is idempotent."""

        if any(
            item is None
            for item in (completed.completed_at, completed.result_id, completed.result_hash)
        ):
            raise ValueError("完成盲测必须携带结果身份、哈希与完成时间")
        with self._engine.begin() as connection:
            payload = connection.execute(
                select(blind_evaluation_claims.c.payload)
                .where(blind_evaluation_claims.c.plan_id == completed.plan_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                raise ValueError("盲测预算尚未认领")
            existing = BlindEvaluationClaim.model_validate(payload)
            if (
                existing.query_id != completed.query_id
                or existing.source_evaluation_id != completed.source_evaluation_id
                or existing.claimed_at != completed.claimed_at
            ):
                raise ValueError("盲测完成事实与原始认领不一致")
            if existing.completed_at is not None:
                if existing != completed:
                    raise ValueError("同一次盲测已经登记了不同结果")
                return existing
            connection.execute(
                update(blind_evaluation_claims)
                .where(blind_evaluation_claims.c.plan_id == completed.plan_id)
                .values(
                    completed_at=completed.completed_at,
                    result_id=completed.result_id,
                    result_hash=completed.result_hash,
                    payload=completed.model_dump(mode="json"),
                )
            )
        return completed

    def register_proposal(self, proposal: ChangeProposal) -> None:
        self._record(
            change_proposals,
            change_proposals.c.proposal_id,
            proposal.proposal_id,
            {
                "proposal_id": proposal.proposal_id,
                "base_version": proposal.base_manifest_id,
                "change_type": proposal.change_type.value,
                "status": "PROPOSED",
                "payload": proposal.model_dump(mode="json"),
            },
        )

    def record_evaluation(self, result: EvaluationResult) -> None:
        self._record(
            evaluation_results,
            evaluation_results.c.evaluation_id,
            result.evaluation_id,
            {
                "evaluation_id": result.evaluation_id,
                "proposal_id": result.proposal_id,
                "plan_id": result.plan_id,
                "candidate_manifest_id": result.candidate_manifest_id,
                "completed_at": result.completed_at,
                "payload": result.model_dump(mode="json"),
            },
        )

    def require_registered_evaluation_target(
        self,
        target: EvaluationTarget,
        *,
        require_current_base: bool = True,
    ) -> None:
        """拒绝由调用方临时拼出的提案、计划或候选版本。"""
        with self._engine.connect() as connection:
            plan_payload = connection.execute(
                select(evaluation_plans.c.payload).where(
                    evaluation_plans.c.plan_id == target.plan.plan_id
                )
            ).scalar_one_or_none()
            proposal_row = connection.execute(
                select(
                    change_proposals.c.status,
                    change_proposals.c.payload,
                    governance_decisions.c.status,
                    governance_decisions.c.payload,
                )
                .join(
                    governance_decisions,
                    governance_decisions.c.decision_id == change_proposals.c.proposal_id,
                )
                .where(change_proposals.c.proposal_id == target.proposal.proposal_id)
            ).one_or_none()
            candidate_row = connection.execute(
                select(release_manifests.c.status, release_manifests.c.payload).where(
                    release_manifests.c.manifest_id == target.candidate.manifest_id
                )
            ).one_or_none()
            champion_row = connection.execute(
                select(release_manifests.c.status).where(
                    release_manifests.c.manifest_id == target.plan.base_manifest_id
                )
            ).one_or_none()
        if plan_payload != target.plan.model_dump(mode="json"):
            raise ValueError("EvaluationPlan 未登记或登记内容不一致")
        expected_proposal = target.proposal.model_dump(mode="json")
        if (
            proposal_row is None
            or proposal_row[0] != "PROPOSED"
            or proposal_row[1] != expected_proposal
            or proposal_row[2] != "PROPOSED"
            or proposal_row[3] != expected_proposal
        ):
            raise ValueError("ChangeProposal 没有通过治理门禁并登记")
        if (
            candidate_row is None
            or candidate_row[0] != "CHALLENGER"
            or candidate_row[1] != target.candidate.model_dump(mode="json")
        ):
            raise ValueError("候选 ReleaseManifest 未登记或内容不一致")
        allowed_base_statuses = (
            {"CHAMPION"} if require_current_base else {"CHAMPION", "PREVIOUS_STABLE"}
        )
        if champion_row is None or champion_row[0] not in allowed_base_statuses:
            raise ValueError("评估计划的基线不是已登记稳定版本")

    def require_registered_release_inputs(
        self,
        *,
        target: EvaluationTarget,
        evaluation: EvaluationResult,
        current_champion: ReleaseManifest,
    ) -> None:
        self.require_registered_evaluation_target(
            target,
            require_current_base=False,
        )
        with self._engine.connect() as connection:
            evaluation_payload = connection.execute(
                select(evaluation_results.c.payload).where(
                    evaluation_results.c.evaluation_id == evaluation.evaluation_id
                )
            ).scalar_one_or_none()
            champion_payloads = tuple(
                connection.execute(
                    select(release_manifests.c.payload).where(
                        release_manifests.c.status == "CHAMPION"
                    )
                ).scalars()
            )
        if evaluation_payload != evaluation.model_dump(mode="json"):
            raise ValueError("EvaluationResult 未登记或登记内容不一致")
        if champion_payloads != (current_champion.model_dump(mode="json"),):
            raise ValueError("ReleaseWorkflow 指定的当前 CHAMPION 不权威")

    def record_release_approval(self, decision: ReleaseApprovalDecision) -> None:
        values = {
            "decision_id": decision.decision_id,
            "evaluation_id": decision.evaluation_id,
            "candidate_manifest_id": decision.candidate_manifest_id,
            "status": decision.status.value,
            "created_at": decision.created_at,
            "payload": decision.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(release_approval_requests).values(**values))
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(release_approval_requests.c.payload).where(
                        release_approval_requests.c.evaluation_id == decision.evaluation_id
                    )
                ).scalar_one_or_none()
            if existing != values["payload"]:
                raise ValueError("同一 EvaluationResult 已存在不同的 Release 决策") from None

    def record_failed_experiment(self, failed: FailedExperiment) -> None:
        self._record(
            failed_experiment_records,
            failed_experiment_records.c.experiment_id,
            failed.experiment_id,
            {
                "experiment_id": failed.experiment_id,
                "hypothesis_fingerprint": failed.hypothesis_fingerprint,
                "rejected_at": failed.rejected_at,
                "payload": failed.model_dump(mode="json"),
            },
        )

    def _record(self, table, key_column, key: str, values: dict) -> None:
        payload = values["payload"]
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(table).values(**values))
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(table.c.payload).where(key_column == key)
                ).scalar_one()
            if existing != payload:
                raise ValueError(f"治理事实 {key} 已存在且内容不同") from None


class SqlEventStore:
    """标准事件事实；新增事件与每品种 Trigger/Outbox 在同一事务提交。"""

    def __init__(
        self,
        engine: Engine,
        *,
        pipeline_id: str = "default",
        trigger_expiry_seconds: int = 900,
        max_visible_events: int = 100,
    ) -> None:
        if trigger_expiry_seconds < 1:
            raise ValueError("事件触发有效期必须为正数")
        if not 1 <= max_visible_events <= 1000:
            raise ValueError("可见事件读取上限必须在 1..1000")
        self._engine = engine
        self._pipeline_id = pipeline_id
        self._trigger_expiry_seconds = trigger_expiry_seconds
        self._max_visible_events = max_visible_events

    def put(self, event: IntelligenceEvent) -> bool:
        payload = event.model_dump(mode="json")
        digest = content_hash({"title": event.title, "body": event.body})
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(normalized_events).values(
                        evidence_id=event.evidence_id,
                        event_time=event.event_time,
                        observed_at=event.observed_at,
                        source=event.source,
                        content_hash=digest,
                        payload=payload,
                    )
                )
                for symbol in event.symbols:
                    trigger = build_trigger_event(
                        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                        symbol=symbol,
                        pipeline_id=self._pipeline_id,
                        occurred_at=event.event_time,
                        observed_at=event.observed_at,
                        priority=int(event.impact * 100),
                        dedup_key=event.evidence_id,
                        evidence_ids=(event.evidence_id,),
                        expires_at=event.observed_at
                        + timedelta(seconds=self._trigger_expiry_seconds),
                    )
                    insert_trigger_with_outbox(connection, trigger)
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(normalized_events.c.payload).where(
                        (normalized_events.c.evidence_id == event.evidence_id)
                        | (
                            (normalized_events.c.source == event.source)
                            & (normalized_events.c.content_hash == digest)
                        )
                    )
                ).scalar_one_or_none()
            if existing is not None and existing != payload:
                same_content = (
                    existing.get("source") == payload["source"]
                    and existing.get("title") == payload["title"]
                    and existing.get("body") == payload["body"]
                )
                if not same_content:
                    raise ValueError("事件唯一键冲突且事实不一致") from None
            return False
        return True

    def visible(self, *, symbol: str, as_of: datetime) -> tuple[IntelligenceEvent, ...]:
        as_of = _require_utc(as_of)
        # Trigger 激活必须按 pipeline 隔离，但已观测到的世界事实不应在每次
        # 发布时清空。这里只复用历史 Trigger 中的品种路由事实，不会让旧
        # pipeline 的触发器、待处理批次或调用准入进入新 pipeline。
        routed_evidence = (
            select(analysis_trigger_events.c.dedup_key.label("evidence_id"))
            .where(
                analysis_trigger_events.c.trigger_type == "INTELLIGENCE_INSERTED",
                analysis_trigger_events.c.symbol == symbol,
                analysis_trigger_events.c.observed_at <= as_of,
            )
            .distinct()
            .subquery()
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(normalized_events.c.payload)
                .select_from(
                    normalized_events.join(
                        routed_evidence,
                        routed_evidence.c.evidence_id == normalized_events.c.evidence_id,
                    )
                )
                .where(
                    normalized_events.c.observed_at <= as_of,
                )
                .order_by(
                    normalized_events.c.event_time.desc(),
                    normalized_events.c.evidence_id.desc(),
                )
                .limit(self._max_visible_events)
            ).scalars()
            events = tuple(IntelligenceEvent.model_validate(item) for item in rows)
        return tuple(sorted(events, key=lambda item: (item.event_time, item.evidence_id)))


class SqlEvaluationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, report: ReplayEvaluationReport) -> None:
        payload = report.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(replay_evaluation_reports).values(
                        report_id=report.report_id,
                        evaluation_version=report.evaluation_version,
                        dataset_hash=report.dataset_hash,
                        statistically_conclusive=report.statistically_conclusive,
                        payload=payload,
                    )
                )
        except IntegrityError:
            existing = self.get(report.report_id)
            if existing != report:
                raise ValueError("相同 report_id 的评估事实不一致") from None

    def get(self, report_id: str) -> ReplayEvaluationReport | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(replay_evaluation_reports.c.payload).where(
                    replay_evaluation_reports.c.report_id == report_id
                )
            ).scalar_one_or_none()
        return ReplayEvaluationReport.model_validate(payload) if payload else None


class SqlLifecycleLedger:
    """持久化独立于分析周期运行的持仓退出和结果归因。"""

    atomic_risk_transition = True

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_lifecycle(self, facts: LifecycleFacts) -> LifecycleFacts:
        existing = self._read_outcome(facts.outcome.outcome_id)
        if existing is not None:
            if existing != facts.outcome:
                raise ValueError("相同 outcome_id 的数据库事实不一致")
            return facts
        try:
            with self._engine.begin() as connection:
                current = (
                    connection.execute(
                        select(position_lifecycles)
                        .where(position_lifecycles.c.position_id == facts.lifecycle.position_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if current["status"] == "CLOSED":
                    concurrent_payload = connection.execute(
                        select(decision_outcomes.c.payload).where(
                            decision_outcomes.c.outcome_id == facts.outcome.outcome_id
                        )
                    ).scalar_one_or_none()
                    if (
                        concurrent_payload is not None
                        and DecisionOutcome.model_validate(concurrent_payload) == facts.outcome
                    ):
                        return facts
                    raise ValueError("持仓已关闭但缺少对应 DecisionOutcome")
                reservation = (
                    connection.execute(
                        select(risk_reservations)
                        .where(risk_reservations.c.reservation_id == facts.lifecycle.reservation_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if reservation["status"] != "CONSUMED":
                    raise ValueError("开放持仓的风险占用不是 CONSUMED")
                budget = (
                    connection.execute(
                        select(portfolio_risk_budgets)
                        .where(portfolio_risk_budgets.c.portfolio_id == "primary")
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                connection.execute(
                    update(risk_reservations)
                    .where(risk_reservations.c.reservation_id == facts.lifecycle.reservation_id)
                    .values(status="RELEASED")
                )
                connection.execute(
                    update(portfolio_risk_budgets)
                    .where(portfolio_risk_budgets.c.portfolio_id == "primary")
                    .values(
                        exposure_risk_amount=(
                            budget["exposure_risk_amount"] - reservation["risk_amount"]
                        )
                    )
                )
                connection.execute(
                    update(position_lifecycles)
                    .where(position_lifecycles.c.position_id == facts.lifecycle.position_id)
                    .values(
                        status=facts.lifecycle.status.value,
                        payload=facts.lifecycle.model_dump(mode="json"),
                    )
                )
                account = facts.account_after_exit
                connection.execute(
                    insert(account_snapshots).values(
                        snapshot_id=_snapshot_id(facts.lifecycle.cycle_id, "POST_EXIT"),
                        cycle_id=facts.lifecycle.cycle_id,
                        phase="POST_EXIT",
                        as_of=account.as_of,
                        content_hash=_payload_hash(account),
                        reconciled=account.reconciled,
                        payload=account.model_dump(mode="json"),
                    )
                )
                _insert_order(
                    connection,
                    facts.exit_order,
                    facts.lifecycle.cycle_id,
                    "EXIT",
                )
                outcome = facts.outcome
                connection.execute(
                    insert(decision_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        cycle_id=facts.lifecycle.cycle_id,
                        intent_id=outcome.intent_id,
                        position_id=outcome.position_id,
                        net_pnl=outcome.net_pnl,
                        payload=outcome.model_dump(mode="json"),
                    )
                )
                _insert_many(
                    connection,
                    metric_observations,
                    (
                        {
                            "metric_id": item.metric_id,
                            "cycle_id": facts.lifecycle.cycle_id,
                            "phase": "OUTCOME",
                            "sequence": sequence,
                            "metric_version": item.metric_version,
                            "observed_at": item.observed_at,
                            "payload": item.model_dump(mode="json"),
                        }
                        for sequence, item in enumerate(facts.metrics)
                    ),
                )
        except IntegrityError:
            concurrent = self._read_outcome(facts.outcome.outcome_id)
            if concurrent != facts.outcome:
                raise
        return facts

    def _read_outcome(self, outcome_id: str) -> DecisionOutcome | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(decision_outcomes.c.payload).where(
                    decision_outcomes.c.outcome_id == outcome_id
                )
            ).scalar_one_or_none()
        return DecisionOutcome.model_validate(payload) if payload else None


class SqlOpenLifecycleRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_open(self) -> tuple[OpenLifecycleRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    position_lifecycles.c.payload,
                    analysis_cycles.c.pipeline_version,
                )
                .join(
                    analysis_cycles,
                    analysis_cycles.c.cycle_id == position_lifecycles.c.cycle_id,
                )
                .where(position_lifecycles.c.status != "CLOSED")
                .order_by(position_lifecycles.c.position_id)
            ).all()
        return tuple(
            OpenLifecycleRecord(
                lifecycle=PositionLifecycle.model_validate(payload),
                pipeline_version=pipeline_version,
            )
            for payload, pipeline_version in rows
        )


class SqlFactLedger:
    """将一个周期的所有事实原子写入；重复 cycle_id 必须内容一致。"""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._clock = clock

    def record(
        self,
        facts: CycleFacts,
        *,
        maximum_total_risk: Decimal | None = None,
    ) -> CycleFacts:
        existing = self.get(facts.cycle_id)
        if existing is not None:
            if existing != facts:
                raise ValueError(f"cycle_id {facts.cycle_id} 已存在且内容不同")
            return existing
        try:
            with self._engine.begin() as connection:
                self._insert_facts(
                    connection,
                    facts,
                    maximum_total_risk=maximum_total_risk,
                )
        except IntegrityError:
            concurrent = self.get(facts.cycle_id)
            if concurrent != facts:
                raise
            return concurrent
        return facts

    def complete_execution(self, result: ExecutionResult) -> CycleFacts:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(execution_requests)
                    .where(execution_requests.c.execution_id == result.execution_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["cycle_id"] != result.cycle_id:
                raise ValueError("ExecutionResult 没有对应的待执行请求")
            if row["status"] == "COMPLETED":
                existing = ExecutionResult.model_validate(row["result_payload"])
                if existing != result:
                    raise ValueError("ExecutionResult 与既有终态不一致")
                return self.get(result.cycle_id)
            request = ExecutionRequest.model_validate(row["payload"])
            reservation = request.risk_decision.reservation
            assert reservation is not None
            budget = (
                connection.execute(
                    select(portfolio_risk_budgets)
                    .where(portfolio_risk_budgets.c.portfolio_id == "primary")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            stored_reservation = (
                connection.execute(
                    select(risk_reservations)
                    .where(risk_reservations.c.reservation_id == reservation.reservation_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if stored_reservation["status"] != "ACTIVE":
                raise ValueError("待执行请求的风险占用不是 ACTIVE")
            reserved_amount = stored_reservation["risk_amount"]
            if result.risk_transition == RiskTransition.CONSUMED:
                reservation_status = "CONSUMED"
                budget_values = {
                    "reserved_amount": budget["reserved_amount"] - reserved_amount,
                    "exposure_risk_amount": (budget["exposure_risk_amount"] + reserved_amount),
                }
            else:
                reservation_status = "RELEASED"
                budget_values = {
                    "reserved_amount": budget["reserved_amount"] - reserved_amount,
                }
            connection.execute(
                update(risk_reservations)
                .where(risk_reservations.c.reservation_id == reservation.reservation_id)
                .values(status=reservation_status)
            )
            connection.execute(
                update(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == "primary")
                .values(**budget_values)
            )
            self._insert_execution_result(connection, result)
            connection.execute(
                update(analysis_cycles)
                .where(analysis_cycles.c.cycle_id == result.cycle_id)
                .values(outcome=result.outcome.value, reason_code=result.reason_code)
            )
            completed_at = max(
                (item.event_time for item in result.order.fills),
                default=request.market.as_of,
            )
            connection.execute(
                update(execution_requests)
                .where(execution_requests.c.execution_id == result.execution_id)
                .values(
                    status="COMPLETED",
                    updated_at=completed_at,
                    result_payload=result.model_dump(mode="json"),
                )
            )
        completed = self.get(result.cycle_id)
        assert completed is not None
        return completed

    def get(self, cycle_id: str) -> CycleFacts | None:
        with self._engine.connect() as connection:
            cycle = (
                connection.execute(
                    select(analysis_cycles).where(analysis_cycles.c.cycle_id == cycle_id)
                )
                .mappings()
                .first()
            )
            if cycle is None:
                return None
            panel_row = connection.execute(
                select(panel_snapshots.c.payload).where(panel_snapshots.c.cycle_id == cycle_id)
            ).scalar_one()
            candidate_rows = connection.execute(
                select(signal_candidates.c.payload)
                .where(signal_candidates.c.cycle_id == cycle_id)
                .order_by(signal_candidates.c.sequence)
            ).scalars()
            proposal_payload = connection.execute(
                select(analysis_proposals.c.payload).where(
                    analysis_proposals.c.cycle_id == cycle_id
                )
            ).scalar_one_or_none()
            intent_payload = connection.execute(
                select(trade_intents.c.payload).where(trade_intents.c.cycle_id == cycle_id)
            ).scalar_one_or_none()
            risk_payload = connection.execute(
                select(risk_decisions.c.payload).where(risk_decisions.c.cycle_id == cycle_id)
            ).scalar_one_or_none()
            execution_request_payload = connection.execute(
                select(execution_requests.c.payload).where(
                    execution_requests.c.cycle_id == cycle_id
                )
            ).scalar_one_or_none()
            order_payload = connection.execute(
                select(orders.c.payload).where(
                    orders.c.cycle_id == cycle_id, orders.c.role == "ENTRY"
                )
            ).scalar_one_or_none()
            account_after_payload = connection.execute(
                select(account_snapshots.c.payload).where(
                    account_snapshots.c.cycle_id == cycle_id,
                    account_snapshots.c.phase == "POST_EXECUTION",
                )
            ).scalar_one_or_none()
            lifecycle_payload = connection.execute(
                select(position_lifecycles.c.payload).where(
                    position_lifecycles.c.cycle_id == cycle_id
                )
            ).scalar_one_or_none()
            exit_order_payload = connection.execute(
                select(orders.c.payload).where(
                    orders.c.cycle_id == cycle_id, orders.c.role == "EXIT"
                )
            ).scalar_one_or_none()
            outcome_payload = connection.execute(
                select(decision_outcomes.c.payload).where(decision_outcomes.c.cycle_id == cycle_id)
            ).scalar_one_or_none()
            metric_rows = connection.execute(
                select(metric_observations.c.payload)
                .where(
                    metric_observations.c.cycle_id == cycle_id,
                    metric_observations.c.phase == "ANALYSIS",
                )
                .order_by(metric_observations.c.sequence)
            ).scalars()
            outcome_metric_rows = connection.execute(
                select(metric_observations.c.payload)
                .where(
                    metric_observations.c.cycle_id == cycle_id,
                    metric_observations.c.phase == "OUTCOME",
                )
                .order_by(metric_observations.c.sequence)
            ).scalars()
            return CycleFacts(
                cycle_id=cycle_id,
                pipeline_version=cycle["pipeline_version"],
                panel=PanelSnapshot.model_validate(panel_row),
                analysis_proposal=(
                    AnalysisProposal.model_validate(proposal_payload) if proposal_payload else None
                ),
                candidates=tuple(SignalCandidate.model_validate(row) for row in candidate_rows),
                intent=TradeIntent.model_validate(intent_payload) if intent_payload else None,
                risk_decision=RiskDecision.model_validate(risk_payload) if risk_payload else None,
                execution_request=(
                    ExecutionRequest.model_validate(execution_request_payload)
                    if execution_request_payload
                    else None
                ),
                order=Order.model_validate(order_payload) if order_payload else None,
                account_after=(
                    AccountSnapshot.model_validate(account_after_payload)
                    if account_after_payload
                    else None
                ),
                position_lifecycle=(
                    PositionLifecycle.model_validate(lifecycle_payload)
                    if lifecycle_payload
                    else None
                ),
                exit_order=(
                    Order.model_validate(exit_order_payload) if exit_order_payload else None
                ),
                decision_outcome=(
                    DecisionOutcome.model_validate(outcome_payload) if outcome_payload else None
                ),
                metrics=tuple(MetricObservation.model_validate(row) for row in metric_rows),
                outcome_metrics=tuple(
                    MetricObservation.model_validate(row) for row in outcome_metric_rows
                ),
                outcome=cycle["outcome"],
                reason_code=cycle["reason_code"],
            )

    @staticmethod
    def _insert_execution_result(
        connection: Connection,
        result: ExecutionResult,
    ) -> None:
        if result.account_after is not None:
            account = result.account_after
            connection.execute(
                insert(account_snapshots).values(
                    snapshot_id=_snapshot_id(result.cycle_id, "POST_EXECUTION"),
                    cycle_id=result.cycle_id,
                    phase="POST_EXECUTION",
                    as_of=account.as_of,
                    content_hash=_payload_hash(account),
                    reconciled=account.reconciled,
                    payload=account.model_dump(mode="json"),
                )
            )
        _insert_order(connection, result.order, result.cycle_id, "ENTRY")
        if result.position_lifecycle is not None:
            lifecycle = result.position_lifecycle
            connection.execute(
                insert(position_lifecycles).values(
                    position_id=lifecycle.position_id,
                    cycle_id=result.cycle_id,
                    intent_id=lifecycle.intent_id,
                    status=lifecycle.status.value,
                    payload=lifecycle.model_dump(mode="json"),
                )
            )
        if result.exit_order is not None:
            _insert_order(connection, result.exit_order, result.cycle_id, "EXIT")
        if result.decision_outcome is not None:
            outcome = result.decision_outcome
            connection.execute(
                insert(decision_outcomes).values(
                    outcome_id=outcome.outcome_id,
                    cycle_id=result.cycle_id,
                    intent_id=outcome.intent_id,
                    position_id=outcome.position_id,
                    net_pnl=outcome.net_pnl,
                    payload=outcome.model_dump(mode="json"),
                )
            )
        existing_metric_count = connection.execute(
            select(func.count())
            .select_from(metric_observations)
            .where(
                metric_observations.c.cycle_id == result.cycle_id,
                metric_observations.c.phase == "ANALYSIS",
            )
        ).scalar_one()
        _insert_many(
            connection,
            metric_observations,
            (
                {
                    "metric_id": item.metric_id,
                    "cycle_id": result.cycle_id,
                    "phase": "ANALYSIS",
                    "sequence": existing_metric_count + sequence,
                    "metric_version": item.metric_version,
                    "observed_at": item.observed_at,
                    "payload": item.model_dump(mode="json"),
                }
                for sequence, item in enumerate(result.metrics)
            ),
        )
        _insert_many(
            connection,
            metric_observations,
            (
                {
                    "metric_id": item.metric_id,
                    "cycle_id": result.cycle_id,
                    "phase": "OUTCOME",
                    "sequence": sequence,
                    "metric_version": item.metric_version,
                    "observed_at": item.observed_at,
                    "payload": item.model_dump(mode="json"),
                }
                for sequence, item in enumerate(result.outcome_metrics)
            ),
        )

    def _insert_facts(
        self,
        connection: Connection,
        facts: CycleFacts,
        *,
        maximum_total_risk: Decimal | None,
    ) -> None:
        panel = facts.panel
        created_at = _require_utc(self._clock())
        connection.execute(
            insert(analysis_cycles).values(
                cycle_id=facts.cycle_id,
                as_of=panel.as_of,
                pipeline_version=facts.pipeline_version,
                outcome=facts.outcome,
                reason_code=facts.reason_code,
                created_at=created_at,
            )
        )
        connection.execute(
            insert(market_snapshots).values(
                cycle_id=facts.cycle_id,
                symbol=panel.market.symbol,
                as_of=panel.market.as_of,
                content_hash=_payload_hash(panel.market),
                payload=panel.market.model_dump(mode="json"),
            )
        )
        connection.execute(
            insert(account_snapshots).values(
                snapshot_id=_snapshot_id(facts.cycle_id, "INPUT"),
                cycle_id=facts.cycle_id,
                phase="INPUT",
                as_of=panel.account.as_of,
                content_hash=_payload_hash(panel.account),
                reconciled=panel.account.reconciled,
                payload=panel.account.model_dump(mode="json"),
            )
        )
        if facts.account_after is not None:
            post_account = facts.account_after
            connection.execute(
                insert(account_snapshots).values(
                    snapshot_id=_snapshot_id(facts.cycle_id, "POST_EXECUTION"),
                    cycle_id=facts.cycle_id,
                    phase="POST_EXECUTION",
                    as_of=post_account.as_of,
                    content_hash=_payload_hash(post_account),
                    reconciled=post_account.reconciled,
                    payload=post_account.model_dump(mode="json"),
                )
            )
        connection.execute(
            insert(panel_snapshots).values(
                cycle_id=facts.cycle_id,
                as_of=panel.as_of,
                schema_version=panel.schema_version,
                policy_version=panel.policy_version,
                content_hash=panel.content_hash,
                payload=panel.model_dump(mode="json"),
            )
        )
        if facts.analysis_proposal is not None:
            proposal = facts.analysis_proposal
            connection.execute(
                insert(analysis_proposals).values(
                    proposal_id=proposal.proposal_id,
                    cycle_id=facts.cycle_id,
                    proposal_type=proposal.proposal_type,
                    suggested_action=proposal.suggested_action.value,
                    forecast_count=len(proposal.forecasts),
                    payload=proposal.model_dump(mode="json"),
                )
            )
        _insert_many(
            connection,
            signal_candidates,
            (
                {
                    "candidate_id": candidate.candidate_id,
                    "cycle_id": facts.cycle_id,
                    "sequence": sequence,
                    "producer_id": candidate.producer_id,
                    "producer_version": candidate.producer_version,
                    "symbol": candidate.symbol,
                    "valid_until": candidate.valid_until,
                    "payload": candidate.model_dump(mode="json"),
                }
                for sequence, candidate in enumerate(facts.candidates)
            ),
        )
        if facts.intent is not None:
            intent = facts.intent
            connection.execute(
                insert(trade_intents).values(
                    intent_id=intent.intent_id,
                    cycle_id=facts.cycle_id,
                    pipeline_version=intent.pipeline_version,
                    symbol=intent.symbol,
                    valid_until=intent.valid_until,
                    payload=intent.model_dump(mode="json"),
                )
            )
        if facts.risk_decision is not None:
            risk = facts.risk_decision
            connection.execute(
                insert(risk_decisions).values(
                    decision_id=risk.decision_id,
                    cycle_id=facts.cycle_id,
                    intent_id=risk.intent_id,
                    outcome=risk.outcome.value,
                    policy_version=risk.policy_version,
                    payload=risk.model_dump(mode="json"),
                )
            )
            if risk.reservation is not None and (
                facts.execution_request is not None or facts.order is not None
            ):
                reservation = risk.reservation
                existing_reservation = connection.execute(
                    select(risk_reservations.c.status).where(
                        risk_reservations.c.reservation_id == reservation.reservation_id
                    )
                ).scalar_one_or_none()
                if existing_reservation is None:
                    if facts.execution_request is not None:
                        if maximum_total_risk is None:
                            raise ValueError("待执行周期必须提供组合风险上限")
                        budget = (
                            connection.execute(
                                select(portfolio_risk_budgets)
                                .where(portfolio_risk_budgets.c.portfolio_id == "primary")
                                .with_for_update()
                            )
                            .mappings()
                            .one()
                        )
                        committed = budget["reserved_amount"] + budget["exposure_risk_amount"]
                        if committed + reservation.risk_amount > maximum_total_risk:
                            raise RiskReservationRejected("PORTFOLIO_RISK_BUDGET_EXHAUSTED")
                    connection.execute(
                        insert(risk_reservations).values(
                            reservation_id=reservation.reservation_id,
                            cycle_id=facts.cycle_id,
                            intent_id=reservation.intent_id,
                            symbol=reservation.symbol,
                            risk_amount=reservation.risk_amount,
                            expires_at=reservation.expires_at,
                            status="CONSUMED" if facts.order else "ACTIVE",
                            payload=reservation.model_dump(mode="json"),
                        )
                    )
                    if facts.execution_request is not None:
                        connection.execute(
                            update(portfolio_risk_budgets)
                            .where(portfolio_risk_budgets.c.portfolio_id == "primary")
                            .values(
                                reserved_amount=(
                                    budget["reserved_amount"] + reservation.risk_amount
                                )
                            )
                        )
        if facts.execution_request is not None:
            request = facts.execution_request
            connection.execute(
                insert(execution_requests).values(
                    execution_id=request.execution_id,
                    cycle_id=request.cycle_id,
                    request_hash=request.request_hash,
                    status="PENDING",
                    created_at=request.created_at,
                    updated_at=request.created_at,
                    payload=request.model_dump(mode="json"),
                    result_payload=None,
                )
            )
        if facts.order is not None:
            _insert_order(connection, facts.order, facts.cycle_id, "ENTRY")
        if facts.position_lifecycle is not None:
            lifecycle = facts.position_lifecycle
            connection.execute(
                insert(position_lifecycles).values(
                    position_id=lifecycle.position_id,
                    cycle_id=facts.cycle_id,
                    intent_id=lifecycle.intent_id,
                    status=lifecycle.status.value,
                    payload=lifecycle.model_dump(mode="json"),
                )
            )
        if facts.exit_order is not None:
            _insert_order(connection, facts.exit_order, facts.cycle_id, "EXIT")
        if facts.decision_outcome is not None:
            outcome = facts.decision_outcome
            connection.execute(
                insert(decision_outcomes).values(
                    outcome_id=outcome.outcome_id,
                    cycle_id=facts.cycle_id,
                    intent_id=outcome.intent_id,
                    position_id=outcome.position_id,
                    net_pnl=outcome.net_pnl,
                    payload=outcome.model_dump(mode="json"),
                )
            )
        _insert_many(
            connection,
            metric_observations,
            (
                {
                    "metric_id": item.metric_id,
                    "cycle_id": facts.cycle_id,
                    "phase": "ANALYSIS",
                    "sequence": sequence,
                    "metric_version": item.metric_version,
                    "observed_at": item.observed_at,
                    "payload": item.model_dump(mode="json"),
                }
                for sequence, item in enumerate(facts.metrics)
            ),
        )
        _insert_many(
            connection,
            metric_observations,
            (
                {
                    "metric_id": item.metric_id,
                    "cycle_id": facts.cycle_id,
                    "phase": "OUTCOME",
                    "sequence": sequence,
                    "metric_version": item.metric_version,
                    "observed_at": item.observed_at,
                    "payload": item.model_dump(mode="json"),
                }
                for sequence, item in enumerate(facts.outcome_metrics)
            ),
        )


def _insert_many(connection: Connection, table: Table, rows: Iterable[dict]) -> None:
    materialized = list(rows)
    if materialized:
        connection.execute(insert(table), materialized)


def _insert_order(connection: Connection, order: Order, cycle_id: str, role: str) -> None:
    connection.execute(
        insert(orders).values(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            cycle_id=cycle_id,
            intent_id=order.intent_id,
            role=role,
            status=order.status.value,
            payload=order.model_dump(mode="json"),
        )
    )
    _insert_many(
        connection,
        fills,
        (
            {
                "fill_id": fill.fill_id,
                "order_id": order.order_id,
                "event_time": fill.event_time,
                "payload": fill.model_dump(mode="json"),
            }
            for fill in order.fills
        ),
    )


def _snapshot_id(cycle_id: str, phase: str) -> str:
    from quant_core.ids import stable_id

    return stable_id("account", cycle_id, phase)


def _payload_hash(value) -> str:
    from quant_core.ids import content_hash

    return content_hash(value)
