from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastTarget,
)
from investment_manager.kernel.identity import SHA256_PATTERN, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
    UnitInterval,
)
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
)


class InstrumentPosition(FrozenModel):
    """Signed economic position for one venue product."""

    instrument: InstrumentId
    quantity: Decimal
    average_price: PositiveDecimal

    @model_validator(mode="after")
    def quantity_must_match_product_capability(self):
        if self.quantity == 0:
            raise ValueError("InstrumentPosition 不保存零数量持仓")
        if self.instrument.product == InstrumentProduct.SPOT and self.quantity < 0:
            raise ValueError("Spot InstrumentPosition 不允许未建模的负持仓")
        return self


class SleevePosition(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    target: ForecastTarget
    legs: tuple[InstrumentPosition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def legs_must_be_a_directionally_valid_target_subset(self):
        leg_by_instrument = {item.instrument.key: item for item in self.target.legs}
        position_keys = tuple(item.instrument.key for item in self.legs)
        if tuple(sorted(set(position_keys))) != position_keys:
            raise ValueError("SleevePosition legs 必须按 Instrument 唯一且排序")
        if not set(position_keys).issubset(leg_by_instrument):
            raise ValueError("SleevePosition 不能包含 ForecastTarget 之外的 Instrument")
        for position in self.legs:
            direction = leg_by_instrument[position.instrument.key].direction
            if (position.quantity > 0) != (direction == ExposureDirection.LONG):
                raise ValueError("SleevePosition quantity 符号必须匹配 Forecast Leg 方向")
        return self


class PortfolioAccountSnapshot(FrozenModel):
    """Authoritative economic account projected from execution facts."""

    snapshot_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    # Ledger ordering is causal metadata, not part of the economic fact hash.
    revision: int = Field(default=0, ge=0, exclude=True)
    as_of: datetime
    observed_at: datetime
    settlement_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    cash_balance: Money
    equity: Money
    equity_high_water: Money
    daily_pnl: Decimal = Decimal("0")
    drawdown_fraction: UnitInterval = Decimal("0")
    positions: tuple[InstrumentPosition, ...] = ()
    sleeves: tuple[SleevePosition, ...] = ()
    pending_execution_group_ids: tuple[str, ...] = ()
    kill_switch_active: bool = False
    reconciled: bool = True

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def account_identity_and_ownership_must_be_consistent(self):
        if self.observed_at > self.as_of:
            raise ValueError("PortfolioAccountSnapshot observed_at 不能晚于 as_of")
        if self.equity_high_water < self.equity:
            raise ValueError("账户权益高水位不能低于当前权益")
        position_keys = tuple(item.instrument.key for item in self.positions)
        if tuple(sorted(set(position_keys))) != position_keys:
            raise ValueError("账户 positions 必须按 Instrument 唯一且排序")
        sleeve_ids = tuple(item.sleeve_id for item in self.sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("账户 sleeves 必须按 sleeve_id 唯一且排序")
        if tuple(sorted(set(self.pending_execution_group_ids))) != (
            self.pending_execution_group_ids
        ):
            raise ValueError("pending execution groups 必须唯一且排序")
        if self.reconciled and self._sleeve_quantities() != {
            item.instrument.key: item.quantity for item in self.positions
        }:
            raise ValueError("已对账账户的 Sleeve 数量之和必须等于产品级净持仓")
        return self

    def _sleeve_quantities(self) -> dict[str, Decimal]:
        quantities: defaultdict[str, Decimal] = defaultdict(Decimal)
        for sleeve in self.sleeves:
            for leg in sleeve.legs:
                quantities[leg.instrument.key] += leg.quantity
        return {
            key: value
            for key, value in sorted(quantities.items())
            if value != 0
        }


class PortfolioPerformanceKind(StrEnum):
    EXECUTION = "EXECUTION"
    MARK_TO_MARKET = "MARK_TO_MARKET"


class PortfolioPerformanceInterval(FrozenModel):
    """Net, fee-inclusive equity change between two causal account facts."""

    interval_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    start_snapshot_id: str = Field(min_length=1)
    end_snapshot_id: str = Field(min_length=1)
    start_as_of: datetime
    end_as_of: datetime
    start_revision: int = Field(ge=0)
    end_revision: int = Field(ge=0)
    kind: PortfolioPerformanceKind
    start_equity: PositiveDecimal
    end_equity: Decimal
    net_pnl: Decimal
    return_fraction: Decimal

    _utc_start_as_of = field_validator("start_as_of")(require_utc)
    _utc_end_as_of = field_validator("end_as_of")(require_utc)

    @classmethod
    def between(
        cls,
        start: PortfolioAccountSnapshot,
        end: PortfolioAccountSnapshot,
    ) -> PortfolioPerformanceInterval:
        if start.portfolio_id != end.portfolio_id:
            raise ValueError("Portfolio Performance 快照必须属于同一账户")
        same_time = start.as_of == end.as_of
        if (
            start.snapshot_id == end.snapshot_id
            or end.as_of < start.as_of
            or end.revision != start.revision + 1
        ):
            raise ValueError("Portfolio Performance 快照因果顺序非法")
        payload = {
            "portfolio_id": end.portfolio_id,
            "start_snapshot_id": start.snapshot_id,
            "end_snapshot_id": end.snapshot_id,
            "start_as_of": start.as_of,
            "end_as_of": end.as_of,
            "start_revision": start.revision,
            "end_revision": end.revision,
            "kind": (
                PortfolioPerformanceKind.EXECUTION
                if same_time
                else PortfolioPerformanceKind.MARK_TO_MARKET
            ),
            "start_equity": start.equity,
            "end_equity": end.equity,
            "net_pnl": end.equity - start.equity,
            "return_fraction": (end.equity - start.equity) / start.equity,
        }
        return cls(
            interval_id=stable_id(
                "portfolio_performance",
                start.snapshot_id,
                end.snapshot_id,
            ),
            **payload,
        )

    @model_validator(mode="after")
    def economics_and_identity_must_reconcile(self):
        same_time = self.start_as_of == self.end_as_of
        if (
            self.start_snapshot_id == self.end_snapshot_id
            or self.end_as_of < self.start_as_of
            or self.end_revision != self.start_revision + 1
        ):
            raise ValueError("Portfolio Performance 时间或 revision 顺序非法")
        expected_kind = (
            PortfolioPerformanceKind.EXECUTION
            if same_time
            else PortfolioPerformanceKind.MARK_TO_MARKET
        )
        if self.kind != expected_kind:
            raise ValueError("Portfolio Performance 类型与时点不一致")
        expected_pnl = self.end_equity - self.start_equity
        if self.net_pnl != expected_pnl or self.return_fraction != (
            expected_pnl / self.start_equity
        ):
            raise ValueError("Portfolio Performance 净收益无法与权益核对")
        if self.interval_id != stable_id(
            "portfolio_performance",
            self.start_snapshot_id,
            self.end_snapshot_id,
        ):
            raise ValueError("Portfolio Performance identity 不一致")
        return self


class SleeveTarget(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    forecast_target: ForecastTarget
    desired_gross_notional: Money
    forecast_ids: tuple[str, ...] = Field(min_length=1)
    conservative_gross_bps: Decimal
    estimated_variable_cost_bps: Money
    conservative_net_bps: Decimal
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @staticmethod
    def identity_for(
        *,
        portfolio_id: str,
        forecast_family: str,
        forecast_target_id: str,
    ) -> str:
        return stable_id(
            "portfolio_sleeve",
            portfolio_id,
            forecast_family,
            forecast_target_id,
        )

    @model_validator(mode="after")
    def economics_and_refs_must_be_consistent(self):
        expected_net = self.conservative_gross_bps - self.estimated_variable_cost_bps
        if self.conservative_net_bps != expected_net:
            raise ValueError("SleeveTarget 净收益必须等于保守毛收益减可变成本")
        if tuple(sorted(set(self.forecast_ids))) != self.forecast_ids:
            raise ValueError("SleeveTarget forecast_ids 必须唯一且排序")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("SleeveTarget reason_codes 必须唯一且排序")
        return self


class PortfolioTarget(FrozenModel):
    target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    as_of: datetime
    valid_until: datetime
    reference_equity: PositiveDecimal
    account_snapshot_id: str = Field(min_length=1)
    account_snapshot_hash: str = Field(pattern=SHA256_PATTERN)
    considered_forecast_ids: tuple[str, ...] = ()
    quotes: tuple[ExecutableQuote, ...] = ()
    sleeves: tuple[SleeveTarget, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def sleeve_set_must_be_bounded_and_unambiguous(self):
        if self.as_of >= self.valid_until:
            raise ValueError("PortfolioTarget 必须具有未来有效期")
        sleeve_ids = tuple(item.sleeve_id for item in self.sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioTarget Sleeves 必须唯一且排序")
        if tuple(sorted(set(self.considered_forecast_ids))) != (
            self.considered_forecast_ids
        ):
            raise ValueError("considered_forecast_ids 必须唯一且排序")
        referenced_forecasts = {
            forecast_id
            for sleeve in self.sleeves
            for forecast_id in sleeve.forecast_ids
        }
        if not referenced_forecasts.issubset(self.considered_forecast_ids):
            raise ValueError("SleeveTarget Forecast 必须来自 Portfolio 考虑集")
        quote_keys = tuple(item.instrument.key for item in self.quotes)
        if tuple(sorted(set(quote_keys))) != quote_keys:
            raise ValueError("PortfolioTarget quotes 必须按 Instrument 唯一且排序")
        required_quote_keys = {
            leg.instrument.key
            for sleeve in self.sleeves
            for leg in sleeve.forecast_target.legs
        }
        if not required_quote_keys.issubset(quote_keys):
            raise ValueError("PortfolioTarget quotes 必须覆盖全部 Sleeve Legs")
        if any(item.as_of != self.as_of for item in self.quotes):
            raise ValueError("PortfolioTarget quotes 必须冻结在目标 as_of")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("PortfolioTarget reason_codes 必须唯一且排序")
        for sleeve in self.sleeves:
            expected_id = SleeveTarget.identity_for(
                portfolio_id=self.portfolio_id,
                forecast_family=sleeve.forecast_family,
                forecast_target_id=sleeve.forecast_target.target_id,
            )
            if sleeve.sleeve_id != expected_id:
                raise ValueError("SleeveTarget sleeve_id 与 Portfolio/ForecastTarget 不一致")
        if sum(item.desired_gross_notional for item in self.sleeves) > (
            self.reference_equity
        ):
            raise ValueError("无杠杆 PortfolioTarget gross notional 不能超过参考权益")
        return self


class CapitalCycleOutcome(StrEnum):
    NO_OPPORTUNITY = "NO_OPPORTUNITY"
    HOLD = "HOLD"
    TARGET_DECIDED = "TARGET_DECIDED"
    OPPORTUNITY_ALREADY_DECIDED = "OPPORTUNITY_ALREADY_DECIDED"
    RISK_EXIT = "RISK_EXIT"


class CapitalCycleRecord(FrozenModel):
    """Immutable receipt of one admitted capital evaluation, including no-op."""

    record_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    pipeline_id: str = Field(min_length=1)
    cause_id: str = Field(min_length=1)
    trigger_batch_id: str | None = Field(default=None, min_length=1)
    symbol: str = Field(min_length=1)
    trigger_types: tuple[str, ...] = ()
    triggered_at: datetime
    evaluated_at: datetime
    decision_cycle_id: str = Field(min_length=1)
    account_snapshot_id: str = Field(min_length=1)
    forecast_ids: tuple[str, ...] = ()
    target_id: str | None = None
    outcome: CapitalCycleOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1)

    _utc_times = field_validator("triggered_at", "evaluated_at")(require_utc)

    @classmethod
    def create(
        cls,
        *,
        portfolio_id: str,
        pipeline_id: str,
        cause_id: str,
        trigger_batch_id: str | None,
        symbol: str,
        trigger_types: tuple[str, ...],
        triggered_at: datetime,
        evaluated_at: datetime,
        decision_cycle_id: str,
        account_snapshot_id: str,
        forecast_ids: tuple[str, ...],
        target_id: str | None,
        outcome: CapitalCycleOutcome,
        reason_codes: tuple[str, ...],
    ) -> CapitalCycleRecord:
        triggered_at = require_utc(triggered_at)
        evaluated_at = require_utc(evaluated_at)
        return cls(
            record_id=stable_id(
                "capital_cycle_record",
                portfolio_id,
                pipeline_id,
                cause_id,
            ),
            portfolio_id=portfolio_id,
            pipeline_id=pipeline_id,
            cause_id=cause_id,
            trigger_batch_id=trigger_batch_id,
            symbol=symbol,
            trigger_types=tuple(sorted(set(trigger_types))),
            triggered_at=triggered_at,
            evaluated_at=evaluated_at,
            decision_cycle_id=decision_cycle_id,
            account_snapshot_id=account_snapshot_id,
            forecast_ids=tuple(sorted(set(forecast_ids))),
            target_id=target_id,
            outcome=outcome,
            reason_codes=tuple(sorted(set(reason_codes))),
        )

    @model_validator(mode="after")
    def identity_and_refs_are_consistent(self):
        if self.record_id != stable_id(
            "capital_cycle_record",
            self.portfolio_id,
            self.pipeline_id,
            self.cause_id,
        ):
            raise ValueError("CapitalCycleRecord identity 不一致")
        if tuple(sorted(set(self.trigger_types))) != self.trigger_types:
            raise ValueError("CapitalCycleRecord trigger_types 必须唯一且排序")
        if self.trigger_batch_id is not None and self.cause_id != self.trigger_batch_id:
            raise ValueError("触发批次产生的 CapitalCycleRecord 必须以 batch_id 为 cause")
        if self.evaluated_at < self.triggered_at:
            raise ValueError("CapitalCycleRecord evaluated_at 不能早于触发时间")
        if tuple(sorted(set(self.forecast_ids))) != self.forecast_ids:
            raise ValueError("CapitalCycleRecord forecast_ids 必须唯一且排序")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("CapitalCycleRecord reason_codes 必须唯一且排序")
        requires_target = self.outcome in {
            CapitalCycleOutcome.TARGET_DECIDED,
            CapitalCycleOutcome.OPPORTUNITY_ALREADY_DECIDED,
            CapitalCycleOutcome.RISK_EXIT,
        }
        if requires_target != (self.target_id is not None):
            raise ValueError("CapitalCycleRecord outcome 与 target_id 不一致")
        return self


def sleeve_gross_notional(
    sleeve: SleevePosition | None,
    *,
    quote_by_instrument: Mapping[str, ExecutableQuote],
) -> Decimal:
    """Conservatively value a Sleeve from product-qualified executable quotes."""

    if sleeve is None:
        return Decimal("0")
    gross = Decimal("0")
    for leg in sleeve.legs:
        quote = quote_by_instrument.get(leg.instrument.key)
        if quote is None:
            raise ValueError("当前 Sleeve 缺少产品级报价")
        price = quote.bid if leg.quantity > 0 else quote.ask
        gross += abs(leg.quantity) * price * leg.instrument.contract_multiplier
    return gross
