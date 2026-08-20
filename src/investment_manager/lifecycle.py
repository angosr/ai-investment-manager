from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from investment_manager.domain import (
    AccountSnapshot,
    DecisionOutcome,
    ExitReason,
    MarketSnapshot,
    MetricObservation,
    Order,
    OrderStatus,
    PositionLifecycle,
    PositionLifecycleStatus,
    RiskDecision,
    TradeIntent,
)
from investment_manager.execution import ExecutionExchange
from investment_manager.exit_policy import program_exit_triggered
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.types import FrozenModel
from investment_manager.ledger import LifecycleFacts, LifecycleLedger
from investment_manager.metrics import observation
from investment_manager.reconciliation import MockReconciler
from investment_manager.risk_budget import RiskBudgetStore


class OpenLifecycleRecord(FrozenModel):
    lifecycle: PositionLifecycle
    pipeline_version: str


class OpenLifecycleRepository(Protocol):
    def list_open(self) -> tuple[OpenLifecycleRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    lifecycle: PositionLifecycle
    account: AccountSnapshot
    exit_order: Order | None = None
    outcome: DecisionOutcome | None = None
    metrics: tuple[MetricObservation, ...] = ()


class PositionLifecycleManager:
    def __init__(
        self,
        *,
        exchange: ExecutionExchange,
        reconciler: MockReconciler,
        risk_budget: RiskBudgetStore | None,
        lifecycle_ledger: LifecycleLedger | None = None,
    ) -> None:
        self._exchange = exchange
        self._reconciler = reconciler
        self._risk_budget = risk_budget
        self._lifecycle_ledger = lifecycle_ledger

    def open(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        entry_order: Order,
        account_after_entry: AccountSnapshot,
        market: MarketSnapshot,
    ) -> LifecycleResult:
        if risk.reservation is None or not entry_order.fills:
            raise ValueError("只有带有效 Reservation 的已成交订单才能创建持仓生命周期")
        quantity = sum((fill.quantity for fill in entry_order.fills), Decimal("0"))
        entry_value = sum((fill.price * fill.quantity for fill in entry_order.fills), Decimal("0"))
        entry_fee = sum((fill.fee for fill in entry_order.fills), Decimal("0"))
        entry_price = entry_value / quantity
        position_id = stable_id("position", intent.intent_id, entry_order.order_id)
        lifecycle = PositionLifecycle(
            position_id=position_id,
            cycle_id=intent.cycle_id,
            intent_id=intent.intent_id,
            entry_order_id=entry_order.order_id,
            reservation_id=risk.reservation.reservation_id,
            symbol=intent.symbol,
            quantity=quantity,
            entry_price=entry_price,
            entry_fee=entry_fee,
            stop_price=intent.stop_price,
            opened_at=market.as_of,
            max_exit_at=market.as_of + timedelta(minutes=intent.max_holding_minutes),
            highest_price=entry_price,
            lowest_price=entry_price,
            status=PositionLifecycleStatus.PROTECTION_PENDING,
            program_exit=intent.program_exit,
        )
        protection_id = self._exchange.register_protection(lifecycle)
        if protection_id is None:
            failed = lifecycle.model_copy(
                update={"status": PositionLifecycleStatus.PROTECTION_FAILED}
            )
            return self.force_exit(
                lifecycle=failed,
                market=market,
                account=account_after_entry,
                reason=ExitReason.PROTECTION_FAILURE,
                pipeline_version=intent.pipeline_version,
            )
        return LifecycleResult(
            lifecycle=lifecycle.model_copy(
                update={
                    "status": PositionLifecycleStatus.PROTECTED,
                    "protection_id": protection_id,
                }
            ),
            account=account_after_entry,
        )

    def evaluate(
        self,
        *,
        lifecycle: PositionLifecycle,
        market: MarketSnapshot,
        account: AccountSnapshot,
        pipeline_version: str,
    ) -> LifecycleResult:
        if lifecycle.status == PositionLifecycleStatus.CLOSED:
            return LifecycleResult(lifecycle=lifecycle, account=account)
        updated = lifecycle.model_copy(
            update={
                "highest_price": max(lifecycle.highest_price, market.last),
                "lowest_price": min(lifecycle.lowest_price, market.last),
            }
        )
        if market.last <= lifecycle.stop_price:
            reason = ExitReason.STOP_LOSS
        elif program_exit_triggered(lifecycle.program_exit, market):
            reason = ExitReason.PROGRAM_SIGNAL
        elif market.as_of >= lifecycle.max_exit_at:
            reason = ExitReason.MAX_HOLDING_TIME
        else:
            return LifecycleResult(lifecycle=updated, account=account)
        return self.force_exit(
            lifecycle=updated,
            market=market,
            account=account,
            reason=reason,
            pipeline_version=pipeline_version,
        )

    def force_exit(
        self,
        *,
        lifecycle: PositionLifecycle,
        market: MarketSnapshot,
        account: AccountSnapshot,
        reason: ExitReason,
        pipeline_version: str,
    ) -> LifecycleResult:
        exit_order = self._exchange.submit_position_exit(
            lifecycle=lifecycle,
            market=market,
            reason=reason,
        )
        if exit_order.status != OrderStatus.FILLED or not exit_order.fills:
            raise ValueError("平仓订单未完全成交，禁止关闭生命周期或释放风险预算")
        exit_quantity = sum((fill.quantity for fill in exit_order.fills), Decimal("0"))
        if exit_quantity != lifecycle.quantity:
            raise ValueError("平仓成交数量与持仓生命周期不一致，必须对账后人工处置")

        account_after_exit = self._reconciler.apply(account, exit_order)
        exit_value = sum((fill.price * fill.quantity for fill in exit_order.fills), Decimal("0"))
        exit_fees = sum((fill.fee for fill in exit_order.fills), Decimal("0"))
        gross_pnl = sum(
            ((fill.price - lifecycle.entry_price) * fill.quantity for fill in exit_order.fills),
            Decimal("0"),
        )
        total_fees = lifecycle.entry_fee + exit_fees
        outcome = DecisionOutcome(
            outcome_id=stable_id("outcome", lifecycle.position_id, exit_order.order_id),
            cycle_id=lifecycle.cycle_id,
            intent_id=lifecycle.intent_id,
            pipeline_version=pipeline_version,
            position_id=lifecycle.position_id,
            symbol=lifecycle.symbol,
            opened_at=lifecycle.opened_at,
            closed_at=market.as_of,
            exit_reason=reason,
            quantity=lifecycle.quantity,
            entry_price=lifecycle.entry_price,
            exit_price=exit_value / exit_quantity,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            net_pnl=gross_pnl - total_fees,
            maximum_favorable_excursion=(lifecycle.highest_price - lifecycle.entry_price)
            * lifecycle.quantity,
            maximum_adverse_excursion=(lifecycle.lowest_price - lifecycle.entry_price)
            * lifecycle.quantity,
        )
        closed = lifecycle.model_copy(
            update={
                "status": PositionLifecycleStatus.CLOSED,
                "closed_at": market.as_of,
                "exit_order_id": exit_order.order_id,
                "exit_reason": reason,
            }
        )
        dimensions = {
            "pipeline": pipeline_version,
            "symbol": lifecycle.symbol,
            "exit_reason": reason.value,
        }
        metrics = tuple(
            observation(
                name,
                value,
                cycle_id=lifecycle.cycle_id,
                observed_at=market.as_of,
                dimensions=dimensions,
            )
            for name, value in (
                ("gross_pnl", outcome.gross_pnl),
                ("net_pnl", outcome.net_pnl),
                ("maximum_favorable_excursion", outcome.maximum_favorable_excursion),
                ("maximum_adverse_excursion", outcome.maximum_adverse_excursion),
                (
                    "holding_minutes",
                    Decimal(str((market.as_of - lifecycle.opened_at).total_seconds() / 60)),
                ),
            )
        )
        result = LifecycleResult(
            lifecycle=closed,
            account=account_after_exit,
            exit_order=exit_order,
            outcome=outcome,
            metrics=metrics,
        )
        if self._lifecycle_ledger is not None and self._lifecycle_ledger.atomic_risk_transition:
            self._lifecycle_ledger.record_lifecycle(
                LifecycleFacts(
                    lifecycle=closed,
                    account_after_exit=account_after_exit,
                    exit_order=exit_order,
                    outcome=outcome,
                    metrics=metrics,
                )
            )
        else:
            if self._risk_budget is not None:
                self._risk_budget.release(lifecycle.reservation_id)
            if self._lifecycle_ledger is not None:
                self._lifecycle_ledger.record_lifecycle(
                    LifecycleFacts(
                        lifecycle=closed,
                        account_after_exit=account_after_exit,
                        exit_order=exit_order,
                        outcome=outcome,
                        metrics=metrics,
                    )
                )
        return result
