from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.configuration import StrictConfig


class FeaturePolicy(StrictConfig):
    version: str
    trend_threshold: Decimal = Decimal("0.003")
    volatility_window: int = Field(default=8, ge=2)


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
        return self
