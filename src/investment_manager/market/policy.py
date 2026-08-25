from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.configuration import StrictConfig
from investment_manager.market.models import InstrumentId, InstrumentProduct


class FeaturePolicy(StrictConfig):
    version: str
    trend_threshold: Decimal = Decimal("0.003")
    volatility_window: int = Field(default=8, ge=2)


class CrossVenueSpotProduct(StrictConfig):
    symbol: str = Field(pattern=r"^[A-Z0-9]+$")
    base_asset: str = Field(pattern=r"^[A-Z0-9]+$")
    quote_asset: str = Field(pattern=r"^[A-Z0-9]+$")
    coinbase_product_id: str = Field(pattern=r"^[A-Z0-9]+-[A-Z0-9]+$")
    kraken_pair: str = Field(pattern=r"^[A-Z0-9/]+$")

    @model_validator(mode="after")
    def product_symbols_must_match(self):
        if self.symbol != f"{self.base_asset}{self.quote_asset}":
            raise ValueError("跨场所现货 symbol 必须由 base_asset 与 quote_asset 组成")
        if self.coinbase_product_id != f"{self.base_asset}-{self.quote_asset}":
            raise ValueError("Coinbase product id 与跨场所现货资产不一致")
        if self.kraken_pair != f"{self.base_asset}/{self.quote_asset}":
            raise ValueError("Kraken pair 与跨场所现货资产不一致")
        return self


class CrossVenueSpotPolicy(StrictConfig):
    version: str
    products: tuple[CrossVenueSpotProduct, ...] = Field(min_length=1, max_length=20)
    poll_seconds: int = Field(default=10, ge=5, le=300)
    maximum_age_seconds: int = Field(default=30, ge=10, le=900)
    coinbase_base_url: str = "https://api.exchange.coinbase.com"
    kraken_base_url: str = "https://api.kraken.com"

    @model_validator(mode="after")
    def sources_and_products_must_be_canonical(self):
        if self.coinbase_base_url != "https://api.exchange.coinbase.com":
            raise ValueError("跨场所现货 Coinbase 必须使用官方生产 REST")
        if self.kraken_base_url != "https://api.kraken.com":
            raise ValueError("跨场所现货 Kraken 必须使用官方生产 REST")
        symbols = tuple(item.symbol for item in self.products)
        if tuple(sorted(set(symbols))) != symbols:
            raise ValueError("跨场所现货 products 必须按 symbol 唯一排序")
        if any(item.quote_asset != "USDT" for item in self.products):
            raise ValueError("跨场所现货只比较同为 USDT 计价的盘口")
        if self.maximum_age_seconds < self.poll_seconds * 2:
            raise ValueError("跨场所现货最大年龄必须容忍至少一次失败轮询")
        return self


class MarketDataPolicy(StrictConfig):
    version: str
    symbols: tuple[str, ...] = Field(
        default=("BTCUSDT", "ETHUSDT"), min_length=1, max_length=20
    )
    interval: str = Field(
        default="5m", pattern=r"^(1m|3m|5m|15m|30m|1h|2h|4h|1d)$"
    )
    bar_window: int = Field(default=64, ge=8, le=1000)
    rest_base_url: str = "https://api.binance.com"
    websocket_base_url: str = "wss://stream.binance.com:9443"
    rest_timeout_seconds: int = Field(default=10, ge=1, le=30)
    stream_silence_seconds: int = Field(default=60, ge=10, le=300)
    planned_reconnect_seconds: int = Field(default=85_800, ge=3600, le=86_300)
    reconnect_initial_seconds: int = Field(default=1, ge=1, le=30)
    reconnect_maximum_seconds: int = Field(default=30, ge=1, le=300)
    quote_persist_interval_ms: int = Field(default=1000, ge=100, le=60_000)
    trade_persist_interval_ms: int = Field(default=1000, ge=100, le=60_000)
    maximum_cross_market_quote_skew_seconds: int = Field(default=15, ge=1, le=300)
    perpetual_instruments: tuple[InstrumentId, ...] = ()
    perpetual_rest_base_url: str = "https://fapi.binance.com"
    perpetual_quote_poll_seconds: int = Field(default=5, ge=1, le=60)
    perpetual_poll_seconds: int = Field(default=300, ge=30, le=3600)
    funding_history_lookback_hours: int = Field(default=720, ge=8, le=720)
    cross_venue_spot: CrossVenueSpotPolicy | None = None

    @property
    def interval_seconds(self) -> int:
        unit_seconds = {"m": 60, "h": 3600, "d": 86_400}
        return int(self.interval[:-1]) * unit_seconds[self.interval[-1]]

    @field_validator("symbols")
    @classmethod
    def symbols_must_be_canonical(cls, symbols: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(symbols)) != len(symbols):
            raise ValueError("行情 symbols 不得重复")
        if any(not symbol.isalnum() or symbol != symbol.upper() for symbol in symbols):
            raise ValueError("行情 symbol 必须是大写字母数字")
        return symbols

    @model_validator(mode="after")
    def public_endpoints_and_backoff_must_be_safe(self):
        endpoint_pairs = {
            ("https://api.binance.com", "wss://stream.binance.com:9443"),
            (
                "https://testnet.binance.vision",
                "wss://stream.testnet.binance.vision",
            ),
        }
        if (self.rest_base_url, self.websocket_base_url) not in endpoint_pairs:
            raise ValueError("行情 REST 与 WebSocket 必须使用同一 Binance 官方环境")
        if self.reconnect_maximum_seconds < self.reconnect_initial_seconds:
            raise ValueError("行情最大重连间隔不得短于初始间隔")
        if self.perpetual_rest_base_url not in {
            "https://fapi.binance.com",
            "https://testnet.binancefuture.com",
        }:
            raise ValueError("永续行情必须使用 Binance USD-M 官方 REST 环境")
        expected_perpetual_url = {
            "https://api.binance.com": "https://fapi.binance.com",
            "https://testnet.binance.vision": "https://testnet.binancefuture.com",
        }[self.rest_base_url]
        if self.perpetual_rest_base_url != expected_perpetual_url:
            raise ValueError("Spot 与 Perpetual 行情必须使用同一 Binance 环境")
        if (
            self.perpetual_instruments
            and self.perpetual_quote_poll_seconds
            > self.maximum_cross_market_quote_skew_seconds
        ):
            raise ValueError("永续报价轮询间隔不得超过跨市场报价偏差上限")
        instrument_keys = tuple(item.key for item in self.perpetual_instruments)
        if tuple(sorted(set(instrument_keys))) != instrument_keys:
            raise ValueError("perpetual_instruments 必须按产品身份唯一且排序")
        if any(
            item.product
            not in {
                InstrumentProduct.USD_M_PERPETUAL,
                InstrumentProduct.TRADFI_PERPETUAL,
            }
            for item in self.perpetual_instruments
        ):
            raise ValueError("perpetual_instruments 只能包含 Binance Perpetual")
        if self.cross_venue_spot is not None:
            cross_symbols = tuple(item.symbol for item in self.cross_venue_spot.products)
            if any(symbol not in self.symbols for symbol in cross_symbols):
                raise ValueError("跨场所现货产品必须属于 Binance Spot 行情范围")
        return self
