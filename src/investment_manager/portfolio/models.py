from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastTarget,
)
from investment_manager.kernel.identity import stable_id
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
    sleeves: tuple[SleeveTarget, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def sleeve_set_must_be_bounded_and_unambiguous(self):
        if self.as_of >= self.valid_until:
            raise ValueError("PortfolioTarget 必须具有未来有效期")
        sleeve_ids = tuple(item.sleeve_id for item in self.sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioTarget Sleeves 必须唯一且排序")
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
