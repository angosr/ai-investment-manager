from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
)


class MarketBar(FrozenModel):
    event_time: datetime
    observed_at: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: Money

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @field_validator("high")
    @classmethod
    def high_must_cover_prices(cls, value: Decimal, info):
        values = info.data
        if "open" in values and value < values["open"]:
            raise ValueError("high 不能低于 open")
        return value

    @field_validator("close")
    @classmethod
    def close_must_be_in_range(cls, value: Decimal, info):
        low = info.data.get("low")
        high = info.data.get("high")
        if low is not None and high is not None and not low <= value <= high:
            raise ValueError("close 必须位于 low 与 high 之间")
        return value


class MarketSnapshot(FrozenModel):
    cycle_id: str
    symbol: str
    as_of: datetime
    observed_at: datetime
    bid: PositiveDecimal
    ask: PositiveDecimal
    last: PositiveDecimal
    bars: tuple[MarketBar, ...] = Field(min_length=2)
    source: str

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @field_validator("observed_at")
    @classmethod
    def observation_must_be_visible(cls, value: datetime, info):
        as_of = info.data.get("as_of")
        if as_of is not None and value > as_of:
            raise ValueError("行情 observed_at 不能晚于 as_of")
        return value

    @field_validator("ask")
    @classmethod
    def ask_not_below_bid(cls, value: Decimal, info):
        bid = info.data.get("bid")
        if bid is not None and value < bid:
            raise ValueError("ask 不能低于 bid")
        return value

    @field_validator("bars")
    @classmethod
    def bars_must_be_visible_and_sorted(
        cls, bars: tuple[MarketBar, ...], info
    ) -> tuple[MarketBar, ...]:
        as_of = info.data.get("as_of")
        if any(left.event_time >= right.event_time for left, right in pairwise(bars)):
            raise ValueError("bars 必须按 event_time 严格递增")
        if as_of is not None and any(
            bar.observed_at > as_of or bar.event_time > as_of for bar in bars
        ):
            raise ValueError("快照不能包含 as_of 之后才观察到的行情")
        return bars


class FeatureSnapshot(FrozenModel):
    cycle_id: str
    symbol: str
    as_of: datetime
    feature_set_version: str
    return_fraction: Decimal
    realized_volatility: Money
    atr: Money
    spread_bps: Money
    volume_ratio: Money
    regime: Literal["TRENDING_UP", "TRENDING_DOWN", "RANGING", "UNKNOWN"]
    market_age_seconds: int = Field(ge=0)

    _utc_as_of = field_validator("as_of")(require_utc)


class MarketQuote(FrozenModel):
    quote_id: str
    symbol: str
    observed_at: datetime
    bid: PositiveDecimal
    bid_quantity: Money
    ask: PositiveDecimal
    ask_quantity: Money
    update_id: int | None = Field(default=None, ge=0)
    source: str

    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def ask_must_not_be_below_bid(self):
        if self.ask < self.bid:
            raise ValueError("ask 不能低于 bid")
        return self


class MarketTrade(FrozenModel):
    trade_id: str
    symbol: str
    aggregate_trade_id: int = Field(ge=0)
    event_time: datetime
    observed_at: datetime
    price: PositiveDecimal
    quantity: PositiveDecimal
    buyer_is_maker: bool
    source: str

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)


class ClosedMarketBar(FrozenModel):
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    observed_at: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: Money
    source: str

    _utc_open_time = field_validator("open_time")(require_utc)
    _utc_close_time = field_validator("close_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def range_and_times_must_be_valid(self):
        if self.close_time <= self.open_time:
            raise ValueError("K 线 close_time 必须晚于 open_time")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("K 线 OHLC 范围非法")
        return self

    def to_market_bar(self) -> MarketBar:
        return MarketBar(
            event_time=self.open_time,
            observed_at=self.observed_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


MarketEvent = MarketQuote | MarketTrade | ClosedMarketBar
