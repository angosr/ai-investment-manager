from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import field_validator, model_validator

from investment_manager.config import ReconciliationPolicy
from investment_manager.execution.models import (
    AccountSnapshot,
    Order,
    OrderStatus,
    Position,
    Side,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel


def _differences_hash(
    differences: tuple[ReconciliationDifference, ...] | list[ReconciliationDifference],
) -> str:
    return content_hash([item.model_dump(mode="json") for item in differences])


class ReconciliationStatus(StrEnum):
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class DifferenceKind(StrEnum):
    SNAPSHOT_STALE = "SNAPSHOT_STALE"
    BALANCE = "BALANCE"
    DAILY_PNL = "DAILY_PNL"
    POSITION_MISSING_LOCAL = "POSITION_MISSING_LOCAL"
    POSITION_MISSING_REMOTE = "POSITION_MISSING_REMOTE"
    POSITION_QUANTITY = "POSITION_QUANTITY"
    POSITION_AVERAGE_PRICE = "POSITION_AVERAGE_PRICE"
    OPEN_ORDER_COUNT = "OPEN_ORDER_COUNT"
    ORDER_MISSING_LOCAL = "ORDER_MISSING_LOCAL"
    ORDER_MISSING_REMOTE = "ORDER_MISSING_REMOTE"
    ORDER_FACT = "ORDER_FACT"


class TradingStateSnapshot(FrozenModel):
    snapshot_id: str
    as_of: datetime
    observed_at: datetime
    account: AccountSnapshot
    orders: tuple[Order, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def visible_and_unique(self):
        if self.observed_at > self.as_of:
            raise ValueError("对账快照 observed_at 不能晚于 as_of")
        client_ids = [item.client_order_id for item in self.orders]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("对账快照 client_order_id 不得重复")
        return self


class ReconciliationDifference(FrozenModel):
    kind: DifferenceKind
    key: str
    local_value: str | None = None
    remote_value: str | None = None


class ReconciliationReport(FrozenModel):
    report_id: str
    as_of: datetime
    policy_version: str
    status: ReconciliationStatus
    freeze_new_risk: bool
    local_snapshot_id: str
    remote_snapshot_id: str
    authoritative_account: AccountSnapshot
    differences: tuple[ReconciliationDifference, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)

    @model_validator(mode="after")
    def state_must_be_consistent(self):
        if self.status == ReconciliationStatus.MATCHED:
            if self.differences or self.freeze_new_risk:
                raise ValueError("MATCHED 报告不得包含差异或冻结新增风险")
        elif not self.freeze_new_risk:
            raise ValueError("非 MATCHED 对账必须冻结新增风险")
        expected_id = stable_id(
            "reconciliation",
            self.as_of.isoformat(),
            self.policy_version,
            self.local_snapshot_id,
            self.remote_snapshot_id,
            _differences_hash(self.differences),
        )
        if self.report_id != expected_id:
            raise ValueError("ReconciliationReport report_id 不一致")
        return self


class TradingStateSource(Protocol):
    def snapshot(self, *, as_of: datetime) -> TradingStateSnapshot: ...


class ReconciliationReportStore(Protocol):
    def record(self, report: ReconciliationReport) -> ReconciliationReport: ...

    def latest(self, *, as_of: datetime) -> ReconciliationReport | None: ...


class ReconciliationEngine:
    def __init__(self, policy: ReconciliationPolicy) -> None:
        self._policy = policy

    def compare(
        self,
        local: TradingStateSnapshot,
        remote: TradingStateSnapshot,
        *,
        as_of: datetime,
    ) -> ReconciliationReport:
        as_of = require_utc(as_of)
        differences: list[ReconciliationDifference] = []
        stale = False
        for name, snapshot in (("local", local), ("remote", remote)):
            age = (as_of - snapshot.observed_at).total_seconds()
            if age > self._policy.maximum_report_age_seconds:
                stale = True
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.SNAPSHOT_STALE,
                        key=name,
                        local_value=str(max(0, int(age))) if name == "local" else None,
                        remote_value=str(max(0, int(age))) if name == "remote" else None,
                    )
                )

        self._compare_account(local.account, remote.account, differences)
        self._compare_orders(local.orders, remote.orders, differences)
        status = (
            ReconciliationStatus.UNKNOWN
            if stale
            else ReconciliationStatus.MISMATCH
            if differences
            else ReconciliationStatus.MATCHED
        )
        report_id = stable_id(
            "reconciliation",
            as_of.isoformat(),
            self._policy.version,
            local.snapshot_id,
            remote.snapshot_id,
            _differences_hash(differences),
        )
        return ReconciliationReport(
            report_id=report_id,
            as_of=as_of,
            policy_version=self._policy.version,
            status=status,
            freeze_new_risk=status != ReconciliationStatus.MATCHED,
            local_snapshot_id=local.snapshot_id,
            remote_snapshot_id=remote.snapshot_id,
            authoritative_account=remote.account,
            differences=tuple(differences),
        )

    def unknown(
        self,
        local: TradingStateSnapshot,
        *,
        as_of: datetime,
        reason_code: str,
    ) -> ReconciliationReport:
        as_of = require_utc(as_of)
        difference = ReconciliationDifference(
            kind=DifferenceKind.SNAPSHOT_STALE,
            key="remote_query",
            remote_value=reason_code,
        )
        remote_snapshot_id = stable_id("remote_unavailable", as_of.isoformat(), reason_code)
        report_id = stable_id(
            "reconciliation",
            as_of.isoformat(),
            self._policy.version,
            local.snapshot_id,
            remote_snapshot_id,
            _differences_hash((difference,)),
        )
        return ReconciliationReport(
            report_id=report_id,
            as_of=as_of,
            policy_version=self._policy.version,
            status=ReconciliationStatus.UNKNOWN,
            freeze_new_risk=True,
            local_snapshot_id=local.snapshot_id,
            remote_snapshot_id=remote_snapshot_id,
            authoritative_account=local.account.model_copy(update={"reconciled": False}),
            differences=(difference,),
        )

    def _compare_account(
        self,
        local: AccountSnapshot,
        remote: AccountSnapshot,
        differences: list[ReconciliationDifference],
    ) -> None:
        if abs(local.quote_balance - remote.quote_balance) > self._policy.balance_tolerance:
            differences.append(
                ReconciliationDifference(
                    kind=DifferenceKind.BALANCE,
                    key="quote_balance",
                    local_value=str(local.quote_balance),
                    remote_value=str(remote.quote_balance),
                )
            )
        if local.open_order_count != remote.open_order_count:
            differences.append(
                ReconciliationDifference(
                    kind=DifferenceKind.OPEN_ORDER_COUNT,
                    key="open_order_count",
                    local_value=str(local.open_order_count),
                    remote_value=str(remote.open_order_count),
                )
            )
        if abs(local.daily_pnl - remote.daily_pnl) > self._policy.balance_tolerance:
            differences.append(
                ReconciliationDifference(
                    kind=DifferenceKind.DAILY_PNL,
                    key="daily_pnl",
                    local_value=str(local.daily_pnl),
                    remote_value=str(remote.daily_pnl),
                )
            )
        local_positions = {item.symbol: item for item in local.positions}
        remote_positions = {item.symbol: item for item in remote.positions}
        for symbol in sorted(local_positions.keys() | remote_positions.keys()):
            local_position = local_positions.get(symbol)
            remote_position = remote_positions.get(symbol)
            if local_position is None:
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.POSITION_MISSING_LOCAL,
                        key=symbol,
                        remote_value=remote_position.model_dump_json(),
                    )
                )
                continue
            if remote_position is None:
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.POSITION_MISSING_REMOTE,
                        key=symbol,
                        local_value=local_position.model_dump_json(),
                    )
                )
                continue
            if (
                abs(local_position.quantity - remote_position.quantity)
                > self._policy.position_quantity_tolerance
            ):
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.POSITION_QUANTITY,
                        key=symbol,
                        local_value=str(local_position.quantity),
                        remote_value=str(remote_position.quantity),
                    )
                )
            if (
                local_position.quantity != 0
                and remote_position.quantity != 0
                and abs(local_position.average_price - remote_position.average_price)
                > self._policy.balance_tolerance
            ):
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.POSITION_AVERAGE_PRICE,
                        key=symbol,
                        local_value=str(local_position.average_price),
                        remote_value=str(remote_position.average_price),
                    )
                )

    @staticmethod
    def _compare_orders(
        local_orders: tuple[Order, ...],
        remote_orders: tuple[Order, ...],
        differences: list[ReconciliationDifference],
    ) -> None:
        local = {item.client_order_id: item for item in local_orders}
        remote = {item.client_order_id: item for item in remote_orders}
        for client_order_id in sorted(local.keys() | remote.keys()):
            local_order = local.get(client_order_id)
            remote_order = remote.get(client_order_id)
            if local_order is None:
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.ORDER_MISSING_LOCAL,
                        key=client_order_id,
                        remote_value=content_hash(remote_order),
                    )
                )
            elif remote_order is None:
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.ORDER_MISSING_REMOTE,
                        key=client_order_id,
                        local_value=content_hash(local_order),
                    )
                )
            elif local_order != remote_order:
                differences.append(
                    ReconciliationDifference(
                        kind=DifferenceKind.ORDER_FACT,
                        key=client_order_id,
                        local_value=content_hash(local_order),
                        remote_value=content_hash(remote_order),
                    )
                )


class MockReconciler:
    """以 Mock 成交事实重建账户，不信任策略或 Intent 中的预计状态。"""

    def apply(self, account: AccountSnapshot, order: Order) -> AccountSnapshot:
        if not order.fills:
            open_delta = 1 if order.status == OrderStatus.NEW else 0
            return account.model_copy(
                update={"open_order_count": account.open_order_count + open_delta}
            )
        positions = {position.symbol: position for position in account.positions}
        balance = account.quote_balance
        daily_pnl = account.daily_pnl
        for fill in order.fills:
            current = positions.get(order.symbol)
            if order.side == Side.BUY:
                total_cost = fill.price * fill.quantity + fill.fee
                if total_cost > balance:
                    raise ValueError("Mock 成交后的账户余额不足，执行事实无效")
                balance -= total_cost
                previous_quantity = current.quantity if current else Decimal("0")
                previous_cost = (
                    current.average_price * previous_quantity if current else Decimal("0")
                )
                new_quantity = previous_quantity + fill.quantity
                positions[order.symbol] = Position(
                    symbol=order.symbol,
                    quantity=new_quantity,
                    average_price=(previous_cost + fill.price * fill.quantity) / new_quantity,
                )
                daily_pnl -= fill.fee
            else:
                if current is None or current.quantity < fill.quantity:
                    raise ValueError("Mock 卖出成交超过本地已对账持仓")
                balance += fill.price * fill.quantity - fill.fee
                daily_pnl += (fill.price - current.average_price) * fill.quantity - fill.fee
                remaining = current.quantity - fill.quantity
                if remaining == 0:
                    positions.pop(order.symbol)
                else:
                    positions[order.symbol] = current.model_copy(update={"quantity": remaining})
        remaining_order = order.status == OrderStatus.PARTIALLY_FILLED
        return AccountSnapshot(
            cycle_id=order.cycle_id,
            as_of=max(fill.event_time for fill in order.fills),
            observed_at=max(fill.event_time for fill in order.fills),
            quote_balance=balance,
            positions=tuple(sorted(positions.values(), key=lambda item: item.symbol)),
            open_order_count=account.open_order_count + (1 if remaining_order else 0),
            daily_pnl=daily_pnl,
            drawdown_fraction=account.drawdown_fraction,
            equity=None,
            equity_high_water=account.equity_high_water,
            kill_switch_active=account.kill_switch_active,
            reconciled=True,
        )
