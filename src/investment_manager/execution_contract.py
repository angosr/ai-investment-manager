from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import field_validator, model_validator

from investment_manager.domain import (
    AccountSnapshot,
    CycleOutcome,
    DecisionOutcome,
    MetricObservation,
    Order,
    OrderStatus,
    PositionLifecycle,
    TradeIntent,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import MarketSnapshot
from investment_manager.risk.models import (
    RiskDecision,
    RiskOutcome,
)


class RiskTransition(StrEnum):
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class ExecutionRequest(FrozenModel):
    execution_id: str
    cycle_id: str
    intent: TradeIntent
    risk_decision: RiskDecision
    market: MarketSnapshot
    account: AccountSnapshot
    execution_policy_version: str
    created_at: datetime
    request_hash: str

    _utc_created_at = field_validator("created_at")(require_utc)

    @model_validator(mode="after")
    def request_must_be_approved_and_self_identifying(self):
        if not all(
            item == self.cycle_id
            for item in (
                self.intent.cycle_id,
                self.risk_decision.cycle_id,
                self.market.cycle_id,
                self.account.cycle_id,
            )
        ):
            raise ValueError("ExecutionRequest 的 cycle_id 必须一致")
        if self.risk_decision.intent_id != self.intent.intent_id:
            raise ValueError("ExecutionRequest 的 RiskDecision 与 Intent 不一致")
        if (
            self.risk_decision.outcome != RiskOutcome.APPROVED
            or self.risk_decision.reservation is None
            or self.risk_decision.quantity is None
        ):
            raise ValueError("ExecutionRequest 只能由已批准且已定量的风险决策生成")
        expected_id = stable_id(
            "execution",
            self.intent.intent_id,
            self.risk_decision.decision_id,
            self.execution_policy_version,
        )
        if self.execution_id != expected_id:
            raise ValueError("ExecutionRequest execution_id 不一致")
        if self.request_hash != content_hash(_request_payload(self)):
            raise ValueError("ExecutionRequest request_hash 不一致")
        return self


class ExecutionResult(FrozenModel):
    execution_id: str
    cycle_id: str
    outcome: CycleOutcome
    reason_code: str
    risk_transition: RiskTransition
    order: Order
    account_after: AccountSnapshot | None = None
    position_lifecycle: PositionLifecycle | None = None
    exit_order: Order | None = None
    decision_outcome: DecisionOutcome | None = None
    metrics: tuple[MetricObservation, ...] = ()
    outcome_metrics: tuple[MetricObservation, ...] = ()
    result_hash: str

    @model_validator(mode="after")
    def result_must_be_terminal_and_consistent(self):
        if self.outcome not in {CycleOutcome.EXECUTED, CycleOutcome.NO_TRADE}:
            raise ValueError("ExecutionResult 必须是执行终态")
        if self.order.cycle_id != self.cycle_id:
            raise ValueError("ExecutionResult Order cycle_id 不一致")
        if self.order.status not in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            raise ValueError("ExecutionResult Order 必须处于执行终态")
        if self.order.fills and self.account_after is None:
            raise ValueError("存在成交时必须包含执行后账户快照")
        if self.position_lifecycle is not None and self.account_after is None:
            raise ValueError("存在持仓生命周期时必须包含执行后账户快照")
        if self.risk_transition == RiskTransition.CONSUMED and not self.order.fills:
            raise ValueError("未成交订单不能消耗持仓风险预算")
        if (
            self.risk_transition == RiskTransition.RELEASED
            and self.position_lifecycle is not None
            and self.position_lifecycle.status.value != "CLOSED"
        ):
            raise ValueError("仍有开放持仓时不能释放风险预算")
        if self.result_hash != content_hash(_result_payload(self)):
            raise ValueError("ExecutionResult result_hash 不一致")
        return self


def _request_payload(request: ExecutionRequest) -> dict[str, object]:
    return {
        "execution_id": request.execution_id,
        "cycle_id": request.cycle_id,
        "intent": request.intent.model_dump(mode="json"),
        "risk_decision": request.risk_decision.model_dump(mode="json"),
        "market": request.market.model_dump(mode="json"),
        "account": request.account.model_dump(mode="json"),
        "execution_policy_version": request.execution_policy_version,
        "created_at": request.created_at.isoformat(),
    }


def _result_payload(result: ExecutionResult) -> dict[str, object]:
    return result.model_dump(mode="json", exclude={"result_hash"})


def build_execution_request(
    *,
    intent: TradeIntent,
    risk_decision: RiskDecision,
    market: MarketSnapshot,
    account: AccountSnapshot,
    execution_policy_version: str,
    created_at: datetime | None = None,
) -> ExecutionRequest:
    execution_id = stable_id(
        "execution",
        intent.intent_id,
        risk_decision.decision_id,
        execution_policy_version,
    )
    values = {
        "execution_id": execution_id,
        "cycle_id": intent.cycle_id,
        "intent": intent,
        "risk_decision": risk_decision,
        "market": market,
        "account": account,
        "execution_policy_version": execution_policy_version,
        "created_at": created_at or market.as_of,
    }
    provisional = ExecutionRequest.model_construct(**values, request_hash="")
    return ExecutionRequest(**values, request_hash=content_hash(_request_payload(provisional)))


def build_execution_result(**values) -> ExecutionResult:
    provisional = ExecutionResult.model_construct(**values, result_hash="")
    return ExecutionResult(**values, result_hash=content_hash(_result_payload(provisional)))
