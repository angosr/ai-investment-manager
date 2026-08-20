from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.domain import (
    AccountSnapshot,
    FrozenModel,
    MarketSnapshot,
    Money,
    OrderType,
    PositiveDecimal,
    Side,
    _require_utc,
    floor_to_step,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.portfolio_risk import ApprovedTarget


class TradePlannerPolicy(FrozenModel):
    version: str = Field(min_length=1)
    managed_symbols: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def managed_symbols_must_be_unique_and_sorted(self):
        if tuple(sorted(set(self.managed_symbols))) != self.managed_symbols:
            raise ValueError("managed_symbols 必须唯一且排序")
        return self


class MarketExecutionSpec(FrozenModel):
    symbol: str = Field(min_length=1)
    quantity_step: PositiveDecimal
    minimum_order_notional: Money


class TargetDelta(FrozenModel):
    symbol: str = Field(min_length=1)
    current_quote_notional: Money
    desired_quote_notional: Money
    delta_quote_notional: Decimal

    @model_validator(mode="after")
    def delta_must_equal_desired_minus_current(self):
        if self.delta_quote_notional != (
            self.desired_quote_notional - self.current_quote_notional
        ):
            raise ValueError("TargetDelta 必须等于目标减当前暴露")
        return self


class PlanningOmission(FrozenModel):
    symbol: str = Field(min_length=1)
    delta_quote_notional: Decimal
    reason_code: str = Field(min_length=1)


class PlannedTrade(FrozenModel):
    intent_id: str = Field(min_length=1)
    approved_target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    planner_policy_version: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    order_type: OrderType = OrderType.MARKET
    quantity: PositiveDecimal
    reference_price: PositiveDecimal
    quote_notional: PositiveDecimal
    reduce_only: bool
    protective_stop_price: PositiveDecimal | None = None
    valid_until: datetime

    _utc_valid_until = field_validator("valid_until")(_require_utc)

    @model_validator(mode="after")
    def direction_and_protection_must_be_consistent(self):
        if self.reduce_only != (self.side == Side.SELL):
            raise ValueError("现货 Planner 只有 SELL 可以是 reduce_only")
        if self.side == Side.BUY and self.protective_stop_price is None:
            raise ValueError("新增风险 PlannedTrade 必须冻结保护止损")
        if self.side == Side.SELL and self.protective_stop_price is not None:
            raise ValueError("减仓 PlannedTrade 不携带新增风险止损")
        if self.quote_notional != self.quantity * self.reference_price:
            raise ValueError("PlannedTrade quote_notional 与数量价格不一致")
        return self


class TradePlan(FrozenModel):
    plan_id: str = Field(min_length=1)
    approved_target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    planner_policy_version: str = Field(min_length=1)
    created_at: datetime
    target_deltas: tuple[TargetDelta, ...]
    trades: tuple[PlannedTrade, ...]
    omissions: tuple[PlanningOmission, ...]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_created_at = field_validator("created_at")(_require_utc)

    @model_validator(mode="after")
    def plan_order_and_identity_must_be_deterministic(self):
        delta_symbols = tuple(item.symbol for item in self.target_deltas)
        if tuple(sorted(set(delta_symbols))) != delta_symbols:
            raise ValueError("TargetDelta 必须按 symbol 唯一且排序")
        ordering = tuple(
            (0 if item.reduce_only else 1, item.symbol) for item in self.trades
        )
        if tuple(sorted(ordering)) != ordering:
            raise ValueError("TradePlan 必须先减仓再新增风险")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != content_hash(payload):
            raise ValueError("TradePlan plan_hash 与内容不一致")
        return self


class TradePlanner:
    """Translate an approved target delta; never reassess economic direction."""

    def __init__(self, policy: TradePlannerPolicy) -> None:
        self._policy = policy

    def plan(
        self,
        *,
        approved: ApprovedTarget,
        account: AccountSnapshot,
        markets: tuple[MarketSnapshot, ...],
        specs: tuple[MarketExecutionSpec, ...],
        as_of: datetime,
    ) -> TradePlan:
        as_of = _require_utc(as_of)
        if approved.valid_until <= as_of:
            raise ValueError("ApprovedTarget 已过期")
        if approved.as_of != as_of:
            raise ValueError("TradePlanner 必须使用 Risk 批准时点")
        if content_hash(account) != approved.account_snapshot_hash:
            raise ValueError("TradePlanner account 与 Risk 批准快照不一致")
        market_by_symbol = self._unique(markets, "MarketSnapshot")
        spec_by_symbol = self._unique(specs, "MarketExecutionSpec")
        approved_market_hashes = set(approved.market_snapshot_hashes)
        position_by_symbol = self._positions(account)
        approved_by_symbol = {item.symbol: item for item in approved.targets}
        if not set(approved_by_symbol).issubset(self._policy.managed_symbols):
            raise ValueError("ApprovedTarget 包含未托管资产")

        deltas: list[TargetDelta] = []
        trades: list[PlannedTrade] = []
        omissions: list[PlanningOmission] = []
        for symbol in self._policy.managed_symbols:
            market = market_by_symbol.get(symbol)
            position = position_by_symbol.get(symbol)
            current_quantity = (
                position.quantity
                if position is not None
                else Decimal("0")
            )
            if current_quantity < 0:
                raise ValueError("现货 TradePlanner 不接受负持仓")
            if current_quantity > 0 and market is None:
                raise ValueError("已有持仓缺少行情，无法安全规划减仓")
            if market is not None and market.observed_at > as_of:
                raise ValueError("TradePlanner 不接受未来行情")
            current_notional = (
                current_quantity * market.bid
                if market is not None
                else Decimal("0")
            )
            approved_asset = approved_by_symbol.get(symbol)
            desired_notional = (
                approved_asset.approved_quote_notional
                if approved_asset is not None
                else current_notional
            )
            delta = TargetDelta(
                symbol=symbol,
                current_quote_notional=current_notional,
                desired_quote_notional=desired_notional,
                delta_quote_notional=desired_notional - current_notional,
            )
            deltas.append(delta)
            if delta.delta_quote_notional == 0:
                continue
            if account.open_order_count > 0:
                omissions.append(
                    PlanningOmission(
                        symbol=symbol,
                        delta_quote_notional=delta.delta_quote_notional,
                        reason_code="OPEN_ORDERS_REQUIRE_RECONCILIATION",
                    )
                )
                continue
            if market is None or symbol not in spec_by_symbol:
                omissions.append(
                    PlanningOmission(
                        symbol=symbol,
                        delta_quote_notional=delta.delta_quote_notional,
                        reason_code="MARKET_OR_EXECUTION_SPEC_MISSING",
                    )
                )
                continue
            if content_hash(market) not in approved_market_hashes:
                raise ValueError("交易行情与 Risk 批准快照不一致")
            planned = self._trade(
                approved=approved,
                approved_asset=approved_asset,
                delta=delta,
                current_quantity=current_quantity,
                market=market,
                spec=spec_by_symbol[symbol],
            )
            if planned is None:
                omissions.append(
                    PlanningOmission(
                        symbol=symbol,
                        delta_quote_notional=delta.delta_quote_notional,
                        reason_code="DELTA_BELOW_EXECUTION_MINIMUM",
                    )
                )
            else:
                trades.append(planned)
        trades.sort(key=lambda item: (not item.reduce_only, item.symbol))
        values = {
            "approved_target_id": approved.approved_target_id,
            "cycle_id": approved.cycle_id,
            "planner_policy_version": self._policy.version,
            "created_at": as_of,
            "target_deltas": tuple(deltas),
            "trades": tuple(trades),
            "omissions": tuple(omissions),
        }
        plan_id = stable_id(
            "trade_plan",
            approved.approved_target_id,
            self._policy.version,
            content_hash(values),
        )
        payload = {"plan_id": plan_id, **values}
        return TradePlan(
            **payload,
            plan_hash=content_hash(payload),
        )

    def _trade(
        self,
        *,
        approved: ApprovedTarget,
        approved_asset,
        delta: TargetDelta,
        current_quantity: Decimal,
        market: MarketSnapshot,
        spec: MarketExecutionSpec,
    ) -> PlannedTrade | None:
        reducing = delta.delta_quote_notional < 0
        price = market.bid if reducing else market.ask
        raw_quantity = (
            current_quantity
            if reducing and delta.desired_quote_notional == 0
            else abs(delta.delta_quote_notional) / price
        )
        if reducing:
            raw_quantity = min(raw_quantity, current_quantity)
        quantity = floor_to_step(raw_quantity, spec.quantity_step)
        quote_notional = quantity * price
        if quantity <= 0 or quote_notional < spec.minimum_order_notional:
            return None
        side = Side.SELL if reducing else Side.BUY
        return PlannedTrade(
            intent_id=stable_id(
                "target_trade_intent",
                approved.approved_target_id,
                delta.symbol,
                side.value,
                str(quantity),
            ),
            approved_target_id=approved.approved_target_id,
            cycle_id=approved.cycle_id,
            planner_policy_version=self._policy.version,
            symbol=delta.symbol,
            side=side,
            quantity=quantity,
            reference_price=price,
            quote_notional=quote_notional,
            reduce_only=reducing,
            protective_stop_price=(
                None
                if reducing or approved_asset is None
                else approved_asset.protective_stop_price
            ),
            valid_until=approved.valid_until,
        )

    @staticmethod
    def _unique(values, label: str):
        by_symbol = {item.symbol: item for item in values}
        if len(by_symbol) != len(values):
            raise ValueError(f"{label} symbol 必须唯一")
        return by_symbol

    @staticmethod
    def _positions(account: AccountSnapshot):
        values = {item.symbol: item for item in account.positions}
        if len(values) != len(account.positions):
            raise ValueError("Account positions symbol 必须唯一")
        return values
