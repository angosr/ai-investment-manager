from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, PositiveDecimal
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
            self.update_id
            if self.update_id is not None
            else self.exchange_time.isoformat()
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
    source: str = Field(min_length=1)

    _utc_exchange_time = field_validator("exchange_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_next_funding = field_validator("next_funding_time")(require_utc)

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
