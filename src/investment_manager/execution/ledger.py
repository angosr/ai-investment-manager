from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from threading import RLock
from typing import Protocol

from investment_manager.execution.contracts import ExecutionRequest, ExecutionResult, RiskTransition
from investment_manager.execution.models import (
    AccountSnapshot,
    Order,
    PositionLifecycle,
)
from investment_manager.governance.evaluation.metrics import MetricObservation
from investment_manager.legacy.models import (
    AnalysisProposal,
    DecisionOutcome,
    SignalCandidate,
    TradeIntent,
)
from investment_manager.risk.budget import InMemoryRiskBudgetStore
from investment_manager.risk.models import RiskDecision
from investment_manager.state.panel import PanelSnapshot


class RiskReservationRejected(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class CycleFacts:
    cycle_id: str
    pipeline_version: str
    panel: PanelSnapshot
    analysis_proposal: AnalysisProposal | None
    candidates: tuple[SignalCandidate, ...]
    intent: TradeIntent | None
    risk_decision: RiskDecision | None
    execution_request: ExecutionRequest | None
    order: Order | None
    account_after: AccountSnapshot | None
    position_lifecycle: PositionLifecycle | None
    exit_order: Order | None
    decision_outcome: DecisionOutcome | None
    metrics: tuple[MetricObservation, ...]
    outcome_metrics: tuple[MetricObservation, ...]
    outcome: str
    reason_code: str


class FactLedger(Protocol):
    def record(
        self,
        facts: CycleFacts,
        *,
        maximum_total_risk: Decimal | None = None,
    ) -> CycleFacts: ...

    def complete_execution(self, result: ExecutionResult) -> CycleFacts: ...

    def get(self, cycle_id: str) -> CycleFacts | None: ...


@dataclass(frozen=True, slots=True)
class LifecycleFacts:
    lifecycle: PositionLifecycle
    account_after_exit: AccountSnapshot
    exit_order: Order
    outcome: DecisionOutcome
    metrics: tuple[MetricObservation, ...]


class LifecycleLedger(Protocol):
    atomic_risk_transition: bool

    def record_lifecycle(self, facts: LifecycleFacts) -> LifecycleFacts: ...


@dataclass(slots=True)
class InMemoryLifecycleLedger:
    atomic_risk_transition: bool = field(default=False, init=False)
    _outcomes: dict[str, LifecycleFacts] = field(default_factory=dict)

    def record_lifecycle(self, facts: LifecycleFacts) -> LifecycleFacts:
        existing = self._outcomes.get(facts.outcome.outcome_id)
        if existing is not None:
            if existing != facts:
                raise ValueError("相同 outcome_id 的生命周期事实不一致")
            return existing
        self._outcomes[facts.outcome.outcome_id] = facts
        return facts

    def get(self, outcome_id: str) -> LifecycleFacts | None:
        return self._outcomes.get(outcome_id)

    @property
    def count(self) -> int:
        return len(self._outcomes)


@dataclass(slots=True)
class InMemoryFactLedger:
    """测试和回放用事实账本；生产仓储将实现同一原子写入语义。"""

    _cycles: dict[str, CycleFacts] = field(default_factory=dict)
    risk_budget: InMemoryRiskBudgetStore = field(default_factory=InMemoryRiskBudgetStore)
    _lock: RLock = field(default_factory=RLock)

    def record(
        self,
        facts: CycleFacts,
        *,
        maximum_total_risk: Decimal | None = None,
    ) -> CycleFacts:
        with self._lock:
            existing = self._cycles.get(facts.cycle_id)
            if existing is not None:
                if existing != facts:
                    raise ValueError(f"cycle_id {facts.cycle_id} 已存在且内容不同")
                return existing
            if facts.execution_request is not None:
                reservation = facts.execution_request.risk_decision.reservation
                assert reservation is not None
                if maximum_total_risk is None:
                    raise ValueError("待执行周期必须提供组合风险上限")
                claim = self.risk_budget.reserve(
                    reservation,
                    maximum_total_risk=maximum_total_risk,
                )
                if not claim.claimed:
                    raise RiskReservationRejected(claim.reason_code)
            self._cycles[facts.cycle_id] = facts
            return facts

    def complete_execution(self, result: ExecutionResult) -> CycleFacts:
        with self._lock:
            existing = self._cycles[result.cycle_id]
            request = existing.execution_request
            if request is None or request.execution_id != result.execution_id:
                raise ValueError("ExecutionResult 没有对应的待执行请求")
            if existing.outcome != "EXECUTION_PENDING":
                completed = self._cycles[result.cycle_id]
                if completed.order != result.order:
                    raise ValueError("ExecutionResult 与既有终态不一致")
                return completed
            reservation = request.risk_decision.reservation
            assert reservation is not None
            if result.risk_transition == RiskTransition.CONSUMED:
                self.risk_budget.consume(reservation.reservation_id)
            else:
                self.risk_budget.release(reservation.reservation_id)
            completed = replace(
                existing,
                order=result.order,
                account_after=result.account_after,
                position_lifecycle=result.position_lifecycle,
                exit_order=result.exit_order,
                decision_outcome=result.decision_outcome,
                metrics=existing.metrics + result.metrics,
                outcome_metrics=result.outcome_metrics,
                outcome=result.outcome.value,
                reason_code=result.reason_code,
            )
            self._cycles[result.cycle_id] = completed
            return completed

    def get(self, cycle_id: str) -> CycleFacts | None:
        with self._lock:
            return self._cycles.get(cycle_id)

    @property
    def count(self) -> int:
        return len(self._cycles)
