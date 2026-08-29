from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
)


class InstrumentProduct(StrEnum):
    SPOT = "SPOT"
    USD_M_PERPETUAL = "USD_M_PERPETUAL"
    TRADFI_PERPETUAL = "TRADFI_PERPETUAL"


class TradFiMarket(StrEnum):
    EQUITY = "EQUITY"
    COMMODITY = "COMMODITY"
    KR_EQUITY = "KR_EQUITY"
    HK_EQUITY = "HK_EQUITY"
    CN_EQUITY = "CN_EQUITY"


class SpotVenue(StrEnum):
    COINBASE = "COINBASE"
    KRAKEN = "KRAKEN"


class InstrumentId(FrozenModel):
    """Product-qualified market identity; a symbol alone is never sufficient."""

    venue: Literal["BINANCE"] = "BINANCE"
    product: InstrumentProduct
    symbol: str = Field(pattern=r"^[A-Z0-9._-]+$")
    base_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    quote_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    settlement_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    contract_multiplier: PositiveDecimal = Decimal("1")
    tradfi_market: TradFiMarket | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def product_contract_must_be_consistent(self):
        if self.base_asset == self.quote_asset:
            raise ValueError("Instrument base_asset 与 quote_asset 必须不同")
        if self.symbol != f"{self.base_asset}{self.quote_asset}":
            raise ValueError("Binance Instrument symbol 必须由 base_asset 与 quote_asset 组成")
        if self.product == InstrumentProduct.SPOT:
            if self.settlement_asset != self.quote_asset:
                raise ValueError("Spot settlement_asset 必须等于 quote_asset")
            if self.contract_multiplier != Decimal("1"):
                raise ValueError("Spot contract_multiplier 必须为 1")
        if self.product in {
            InstrumentProduct.USD_M_PERPETUAL,
            InstrumentProduct.TRADFI_PERPETUAL,
        } and self.settlement_asset != self.quote_asset:
            raise ValueError("Binance Perpetual settlement_asset 必须等于 quote_asset")
        if (self.product == InstrumentProduct.TRADFI_PERPETUAL) != (
            self.tradfi_market is not None
        ):
            raise ValueError("TradFi Perpetual 必须且只能声明官方交易日历")
        return self

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.product.value}:{self.symbol}"

    @classmethod
    def binance_spot(
        cls,
        *,
        symbol: str,
        base_asset: str,
        quote_asset: str,
    ) -> InstrumentId:
        return cls(
            product=InstrumentProduct.SPOT,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            settlement_asset=quote_asset,
        )


class MarketBar(FrozenModel):
    event_time: datetime
    observed_at: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: Money
    quote_volume: Money | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    taker_buy_base_volume: Money | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    taker_buy_quote_volume: Money | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

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


class MarketReferencePrice(FrozenModel):
    """First-seen exchange reference price; never an executable quote."""

    reference_price_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^[A-Z0-9]+$")
    exchange_time: datetime
    observed_at: datetime
    price: PositiveDecimal
    source: Literal["binance-spot-reference-price-websocket"]

    _utc_exchange_time = field_validator("exchange_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_visibility_must_match(self):
        if self.exchange_time > self.observed_at:
            raise ValueError("ReferencePrice exchange_time 不能晚于首次观察时间")
        if self.reference_price_id != stable_id(
            "binance_spot_reference_price",
            self.symbol,
            self.exchange_time.isoformat(),
        ):
            raise ValueError("ReferencePrice 身份与交易所时间不一致")
        return self


class CrossVenueSpotQuote(FrozenModel):
    """Immutable first-seen L1 quote from an independent spot venue."""

    quote_id: str = Field(min_length=1)
    venue: SpotVenue
    symbol: str = Field(pattern=r"^[A-Z0-9]+$")
    provider_symbol: str = Field(min_length=1)
    exchange_time: datetime
    observed_at: datetime
    bid: PositiveDecimal
    bid_quantity: Money
    ask: PositiveDecimal
    ask_quantity: Money
    source_sequence: str | None = Field(default=None, min_length=1)
    source: str = Field(min_length=1)

    _utc_exchange_time = field_validator("exchange_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_timing_must_be_valid(self):
        if self.exchange_time > self.observed_at:
            raise ValueError("跨场所现货 exchange_time 不能晚于首次观察时间")
        if self.ask < self.bid:
            raise ValueError("跨场所现货 ask 不能低于 bid")
        marker = self.source_sequence or self.exchange_time.isoformat()
        if self.quote_id != stable_id(
            "cross_venue_spot_quote",
            self.venue.value,
            self.symbol,
            marker,
        ):
            raise ValueError("跨场所现货 quote_id 与来源身份不一致")
        expected_source = {
            SpotVenue.COINBASE: "coinbase-exchange-rest",
            SpotVenue.KRAKEN: "kraken-spot-rest",
        }[self.venue]
        if self.source != expected_source:
            raise ValueError("跨场所现货来源与 venue 不一致")
        return self


class ValuationQuoteQuality(StrEnum):
    LIVE_MARKET = "LIVE_MARKET"
    CLOSED_MARKET = "CLOSED_MARKET"
    STALE_MARKET = "STALE_MARKET"


class ValuationQuote(FrozenModel):
    """Exit-side valuation fact; it grants no order authority."""

    source_quote_id: str = Field(min_length=1)
    instrument: InstrumentId
    as_of: datetime
    observed_at: datetime
    bid: PositiveDecimal
    ask: PositiveDecimal
    source: str = Field(min_length=1)
    quality: ValuationQuoteQuality = Field(
        default=ValuationQuoteQuality.LIVE_MARKET,
        exclude_if=lambda value: value == ValuationQuoteQuality.LIVE_MARKET,
    )
    trading_schedule_ref: str | None = Field(
        default=None,
        min_length=1,
        exclude_if=lambda value: value is None,
    )

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def quote_must_be_visible_and_product_qualified(self):
        if self.observed_at > self.as_of:
            raise ValueError("ValuationQuote observed_at 不能晚于 as_of")
        if self.ask < self.bid:
            raise ValueError("ValuationQuote ask 不能低于 bid")
        tradfi = self.instrument.product == InstrumentProduct.TRADFI_PERPETUAL
        if tradfi and self.quality != ValuationQuoteQuality.STALE_MARKET:
            if self.trading_schedule_ref is None:
                raise ValueError("TradFi ValuationQuote 必须引用点时交易日历")
        elif not tradfi and self.trading_schedule_ref is not None:
            raise ValueError("非 TradFi ValuationQuote 不得伪造交易日历引用")
        return self


class ExecutableQuote(ValuationQuote):
    """Live, product-qualified bid/ask frozen for one capital decision."""

    bid_quantity: Money
    ask_quantity: Money

    @model_validator(mode="after")
    def quote_must_be_executable(self):
        if self.quality != ValuationQuoteQuality.LIVE_MARKET:
            raise ValueError("ExecutableQuote 必须来自开放且新鲜的市场")
        if self.bid_quantity <= 0 or self.ask_quantity <= 0:
            raise ValueError("ExecutableQuote 双边可成交数量必须为正数")
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
    quote_volume: Money | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    taker_buy_base_volume: Money | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    taker_buy_quote_volume: Money | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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
        flow_values = (
            self.quote_volume,
            self.taker_buy_base_volume,
            self.taker_buy_quote_volume,
        )
        if any(item is not None for item in flow_values) and not all(
            item is not None for item in flow_values
        ):
            raise ValueError("K 线现货成交摘要必须完整或全部缺省")
        if self.taker_buy_base_volume is not None and self.taker_buy_base_volume > self.volume:
            raise ValueError("K 线主动买入基础资产量不能超过总成交量")
        if (
            self.taker_buy_quote_volume is not None
            and self.quote_volume is not None
            and self.taker_buy_quote_volume > self.quote_volume
        ):
            raise ValueError("K 线主动买入报价资产量不能超过总成交额")
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
            quote_volume=self.quote_volume,
            taker_buy_base_volume=self.taker_buy_base_volume,
            taker_buy_quote_volume=self.taker_buy_quote_volume,
        )


MarketEvent = MarketQuote | MarketReferencePrice | MarketTrade | ClosedMarketBar
