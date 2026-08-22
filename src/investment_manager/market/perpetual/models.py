from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import InstrumentId, InstrumentProduct


class FundingRateType(StrEnum):
    REGULAR = "REGULAR"
    SPECIAL = "SPECIAL"


class PerpetualQuote(FrozenModel):
    quote_id: str = Field(min_length=1)
    instrument: InstrumentId
    exchange_time: datetime
    observed_at: datetime
    bid: PositiveDecimal
    bid_quantity: Decimal = Field(ge=0)
    ask: PositiveDecimal
    ask_quantity: Decimal = Field(ge=0)
    update_id: int | None = Field(default=None, ge=0)
    source: str = Field(min_length=1)

    _utc_exchange_time = field_validator("exchange_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def quote_identity_and_bounds_must_be_valid(self):
        if self.instrument.product == InstrumentProduct.SPOT:
            raise ValueError("PerpetualQuote 不能引用 Spot Instrument")
        if self.exchange_time > self.observed_at:
            raise ValueError("PerpetualQuote exchange_time 不能晚于 observed_at")
        if self.ask < self.bid:
            raise ValueError("PerpetualQuote ask 不能低于 bid")
        marker: str | int = (
            self.update_id if self.update_id is not None else self.exchange_time.isoformat()
        )
        if self.quote_id != stable_id(
            "perpetual_quote",
            self.instrument.key,
            marker,
        ):
            raise ValueError("PerpetualQuote quote_id 与来源身份不一致")
        return self


class PerpetualMarketState(FrozenModel):
    state_id: str = Field(min_length=1)
    instrument: InstrumentId
    exchange_time: datetime
    observed_at: datetime
    mark_price: PositiveDecimal
    index_price: PositiveDecimal
    estimated_settle_price: PositiveDecimal | None = None
    last_funding_rate: Decimal
    interest_rate: Decimal
    next_funding_time: datetime
    positioning_observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    positioning_window_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1_440,
        exclude_if=lambda value: value is None,
    )
    open_interest: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    open_interest_value: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    open_interest_change_fraction: Decimal | None = Field(
        default=None,
        gt=-1,
        exclude_if=lambda value: value is None,
    )
    global_long_short_account_ratio: Decimal | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
    )
    global_long_account_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    global_short_account_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    taker_buy_sell_ratio: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    taker_buy_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    taker_sell_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    source: str = Field(min_length=1)

    _utc_exchange_time = field_validator("exchange_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_next_funding = field_validator("next_funding_time")(require_utc)
    _utc_positioning_observed = field_validator("positioning_observed_at")(optional_utc)

    @model_validator(mode="after")
    def derivative_timing_and_identity_must_be_valid(self):
        if self.instrument.product == InstrumentProduct.SPOT:
            raise ValueError("PerpetualMarketState 不能引用 Spot Instrument")
        if not self.exchange_time <= self.observed_at < self.next_funding_time:
            raise ValueError("PerpetualMarketState 时间顺序非法")
        if self.state_id != stable_id(
            "perpetual_market_state",
            self.instrument.key,
            self.exchange_time.isoformat(),
        ):
            raise ValueError("PerpetualMarketState state_id 与来源身份不一致")
        positioning_values = (
            self.positioning_observed_at,
            self.positioning_window_minutes,
            self.open_interest,
            self.open_interest_value,
            self.global_long_short_account_ratio,
            self.global_long_account_fraction,
            self.global_short_account_fraction,
            self.taker_buy_sell_ratio,
            self.taker_buy_volume,
            self.taker_sell_volume,
        )
        if any(item is not None for item in positioning_values) and not all(
            item is not None for item in positioning_values
        ):
            raise ValueError("衍生品仓位摘要必须完整或全部缺省")
        if (
            self.positioning_observed_at is not None
            and self.positioning_observed_at > self.observed_at
        ):
            raise ValueError("衍生品仓位摘要不能晚于系统观察时间")
        if (
            self.global_long_account_fraction is not None
            and self.global_short_account_fraction is not None
            and abs(
                self.global_long_account_fraction
                + self.global_short_account_fraction
                - Decimal("1")
            )
            > Decimal("0.01")
        ):
            raise ValueError("全市场多空账户占比不完整")
        return self

    @property
    def premium_fraction(self) -> Decimal:
        return self.mark_price / self.index_price - Decimal("1")


class FundingSettlement(FrozenModel):
    settlement_id: str = Field(min_length=1)
    instrument: InstrumentId
    funding_time: datetime
    observed_at: datetime
    funding_rate: Decimal
    mark_price: PositiveDecimal
    rate_type: FundingRateType
    source: str = Field(min_length=1)

    _utc_funding_time = field_validator("funding_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def settlement_timing_and_identity_must_be_valid(self):
        if self.instrument.product == InstrumentProduct.SPOT:
            raise ValueError("FundingSettlement 不能引用 Spot Instrument")
        if self.observed_at <= self.funding_time:
            raise ValueError("FundingSettlement 必须在结算发生后才可见")
        if self.settlement_id != stable_id(
            "funding_settlement",
            self.instrument.key,
            self.funding_time.isoformat(),
            self.rate_type.value,
        ):
            raise ValueError("FundingSettlement settlement_id 与来源身份不一致")
        return self


class DerivativeContextSnapshot(FrozenModel):
    """Compact point-in-time derivatives state for decisions, never raw history."""

    cycle_id: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    instrument: InstrumentId
    as_of: datetime
    observed_at: datetime
    mark_index_premium_bps: Decimal
    executable_short_basis_bps: Decimal
    perpetual_spread_bps: Money
    last_funding_rate_bps: Decimal
    trailing_funding_rate_mean_bps: Decimal | None = None
    trailing_funding_rate_sum_bps: Decimal | None = None
    trailing_funding_rate_stddev_bps: Decimal | None = None
    trailing_funding_positive_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
    )
    trailing_funding_rate_min_bps: Decimal | None = None
    funding_settlement_count: int = Field(ge=0)
    funding_window_hours: int = Field(gt=0, le=720)
    next_funding_time: datetime
    spot_flow_observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    spot_flow_window_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1_440,
        exclude_if=lambda value: value is None,
    )
    spot_taker_buy_sell_ratio: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    spot_taker_buy_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    spot_taker_sell_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    positioning_observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    positioning_window_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1_440,
        exclude_if=lambda value: value is None,
    )
    open_interest: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    open_interest_value: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    open_interest_change_fraction: Decimal | None = Field(
        default=None,
        gt=-1,
        exclude_if=lambda value: value is None,
    )
    global_long_short_account_ratio: Decimal | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
    )
    global_long_account_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    global_short_account_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    taker_buy_sell_ratio: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    taker_buy_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    taker_sell_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    input_refs: tuple[str, ...] = Field(min_length=3)

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_next_funding = field_validator("next_funding_time")(require_utc)
    _utc_spot_flow_observed = field_validator("spot_flow_observed_at")(optional_utc)
    _utc_positioning_observed = field_validator("positioning_observed_at")(optional_utc)

    @model_validator(mode="after")
    def timing_and_funding_summary_must_be_consistent(self):
        if self.instrument.product == InstrumentProduct.SPOT:
            raise ValueError("DerivativeContextSnapshot 不能引用 Spot Instrument")
        if self.observed_at > self.as_of:
            raise ValueError("衍生品决策状态不能晚于 as_of")
        if tuple(sorted(set(self.input_refs))) != self.input_refs:
            raise ValueError("衍生品决策状态 input_refs 必须唯一且排序")
        spot_flow_values = (
            self.spot_flow_observed_at,
            self.spot_flow_window_minutes,
            self.spot_taker_buy_sell_ratio,
            self.spot_taker_buy_volume,
            self.spot_taker_sell_volume,
        )
        if any(item is not None for item in spot_flow_values) and not all(
            item is not None for item in spot_flow_values
        ):
            raise ValueError("现货主动成交摘要必须完整或全部缺省")
        if self.spot_flow_observed_at is not None and self.spot_flow_observed_at > self.as_of:
            raise ValueError("现货主动成交摘要不能晚于 as_of")
        has_summary = (
            self.trailing_funding_rate_mean_bps is not None
            and self.trailing_funding_rate_sum_bps is not None
        )
        if has_summary != (self.funding_settlement_count > 0):
            raise ValueError("Funding 汇总与结算样本数不一致")
        extended_summary = (
            self.trailing_funding_rate_stddev_bps,
            self.trailing_funding_positive_fraction,
            self.trailing_funding_rate_min_bps,
        )
        if any(item is not None for item in extended_summary) and not all(
            item is not None for item in extended_summary
        ):
            raise ValueError("扩展 Funding 汇总必须完整或全部缺省")
        if any(item is not None for item in extended_summary) and not has_summary:
            raise ValueError("扩展 Funding 汇总不能脱离基础汇总")
        positioning_values = (
            self.positioning_observed_at,
            self.positioning_window_minutes,
            self.open_interest,
            self.open_interest_value,
            self.global_long_short_account_ratio,
            self.global_long_account_fraction,
            self.global_short_account_fraction,
            self.taker_buy_sell_ratio,
            self.taker_buy_volume,
            self.taker_sell_volume,
        )
        if any(item is not None for item in positioning_values) and not all(
            item is not None for item in positioning_values
        ):
            raise ValueError("决策仓位摘要必须完整或全部缺省")
        if (
            self.positioning_observed_at is not None
            and self.positioning_observed_at > self.observed_at
        ):
            raise ValueError("决策仓位摘要不能晚于衍生品观察时间")
        if (
            self.global_long_account_fraction is not None
            and self.global_short_account_fraction is not None
            and abs(
                self.global_long_account_fraction
                + self.global_short_account_fraction
                - Decimal("1")
            )
            > Decimal("0.01")
        ):
            raise ValueError("决策仓位摘要的多空账户占比不完整")
        return self
