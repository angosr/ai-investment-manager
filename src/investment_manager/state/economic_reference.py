"""Point-in-time observation-only economic references for mandate assets."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, PositiveDecimal
from investment_manager.market.models import InstrumentId, InstrumentProduct, MarketSnapshot
from investment_manager.market.perpetual.models import (
    PerpetualMarketState,
    PerpetualQuote,
    TradingScheduleSnapshot,
    TradingSessionType,
)


class ObservationReference(FrozenModel):
    """A non-investable comparison instrument attached to one observed asset."""

    target_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    reference_instrument_key: str = Field(min_length=1)
    target_price_per_reference_price: PositiveDecimal = Decimal("1")


class EconomicReferenceSnapshot(FrozenModel):
    """Frozen target/reference relationship with no Forecast or order identity."""

    target_asset: str
    target_market_symbol: str
    reference_instrument: InstrumentId
    as_of: datetime
    target_observed_at: datetime
    reference_state_exchange_time: datetime
    reference_state_observed_at: datetime
    reference_quote_exchange_time: datetime
    reference_quote_observed_at: datetime
    trading_schedule_ref: str = Field(min_length=1)
    session_type: TradingSessionType
    target_mid_price: PositiveDecimal
    reference_index_price: PositiveDecimal
    reference_mark_index_premium_bps: Decimal
    reference_spread_bps: Decimal = Field(ge=0)
    target_price_per_reference_price: PositiveDecimal
    target_reference_deviation_bps: Decimal

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_target_observed = field_validator("target_observed_at")(require_utc)
    _utc_state_exchange = field_validator("reference_state_exchange_time")(require_utc)
    _utc_state_observed = field_validator("reference_state_observed_at")(require_utc)
    _utc_quote_exchange = field_validator("reference_quote_exchange_time")(require_utc)
    _utc_quote_observed = field_validator("reference_quote_observed_at")(require_utc)

    @model_validator(mode="after")
    def reference_is_point_in_time_and_observation_only(self):
        if self.reference_instrument.product != InstrumentProduct.TRADFI_PERPETUAL:
            raise ValueError("经济交叉参考必须使用带官方交易时段的 TradFi Perpetual")
        if max(
            self.target_observed_at,
            self.reference_state_exchange_time,
            self.reference_state_observed_at,
            self.reference_quote_exchange_time,
            self.reference_quote_observed_at,
        ) > self.as_of:
            raise ValueError("经济交叉参考不能包含 as_of 之后的行情")
        return self


def build_economic_reference_snapshot(
    *,
    policy: ObservationReference,
    target: MarketSnapshot,
    reference_state: PerpetualMarketState,
    reference_quote: PerpetualQuote,
    schedule: TradingScheduleSnapshot,
) -> EconomicReferenceSnapshot:
    instrument = reference_state.instrument
    if reference_quote.instrument != instrument:
        raise ValueError("经济交叉参考的 state 与 quote Instrument 不一致")
    if policy.reference_instrument_key != instrument.key:
        raise ValueError("经济交叉参考与冻结政策 Instrument 不一致")
    session = schedule.session_at(instrument=instrument, at=target.as_of)
    if session is None:
        raise ValueError("经济交叉参考缺少覆盖截止时点的交易时段")
    target_mid = (target.bid + target.ask) / Decimal("2")
    reference_mid = (reference_quote.bid + reference_quote.ask) / Decimal("2")
    converted_reference = (
        reference_state.index_price * policy.target_price_per_reference_price
    )
    return EconomicReferenceSnapshot(
        target_asset=policy.target_asset,
        target_market_symbol=target.symbol,
        reference_instrument=instrument,
        as_of=target.as_of,
        target_observed_at=target.observed_at,
        reference_state_exchange_time=reference_state.exchange_time,
        reference_state_observed_at=reference_state.observed_at,
        reference_quote_exchange_time=reference_quote.exchange_time,
        reference_quote_observed_at=reference_quote.observed_at,
        trading_schedule_ref=schedule.schedule_id,
        session_type=session.session_type,
        target_mid_price=target_mid,
        reference_index_price=reference_state.index_price,
        reference_mark_index_premium_bps=(
            (reference_state.mark_price / reference_state.index_price - Decimal("1"))
            * Decimal("10000")
        ),
        reference_spread_bps=(
            (reference_quote.ask - reference_quote.bid)
            / reference_mid
            * Decimal("10000")
        ),
        target_price_per_reference_price=policy.target_price_per_reference_price,
        target_reference_deviation_bps=(
            (target_mid / converted_reference - Decimal("1")) * Decimal("10000")
        ),
    )


__all__ = [
    "EconomicReferenceSnapshot",
    "ObservationReference",
    "build_economic_reference_snapshot",
]
