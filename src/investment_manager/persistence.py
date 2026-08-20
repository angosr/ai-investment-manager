from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

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
    case,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.analyst import AttemptAudit, CapacitySnapshot, CodexLease
from investment_manager.execution.contracts import (
    ExecutionRequest,
    ExecutionResult,
    RiskTransition,
)
from investment_manager.execution.ledger import CycleFacts, LifecycleFacts, RiskReservationRejected
from investment_manager.execution.lifecycle import OpenLifecycleRecord
from investment_manager.execution.models import (
    AccountSnapshot,
    Order,
    PositionLifecycle,
)
from investment_manager.execution.tables import (
    execution_requests,
    fills,
    orders,
    position_lifecycles,
)
from investment_manager.governance.metrics import MetricObservation
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.legacy.models import (
    AnalysisProposal,
    DecisionOutcome,
    SignalCandidate,
    TradeIntent,
)
from investment_manager.platform.database import metadata
from investment_manager.risk.budget import (
    portfolio_risk_budgets,
    risk_reservations,
)
from investment_manager.risk.models import RiskDecision
from investment_manager.state.panel import PanelSnapshot

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
        if attempt.analysis_behavior_hash is not None:
            payload["analysis_behavior_hash"] = attempt.analysis_behavior_hash
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
        created_at = require_utc(self._clock())
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
    from investment_manager.kernel.identity import stable_id

    return stable_id("account", cycle_id, phase)


def _payload_hash(value) -> str:

    return content_hash(value)
