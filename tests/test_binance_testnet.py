from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from investment_manager.execution.venue.binance_client import (
    BinanceCredentials,
    BinanceTestnetClient,
    SymbolRules,
)


def _exchange_info():
    return {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"],
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": "0.01",
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.00001",
                "maxQty": "1000",
                "stepSize": "0.00001",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "0",
                "maxQty": "100",
                "stepSize": "0",
            },
            {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True},
        ],
    }


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers, timeout_seconds):
        self.calls.append((method, url, headers, timeout_seconds))
        if urlparse(url).path.endswith("/v3/time"):
            return 200, {"serverTime": 1_000_000}
        if urlparse(url).path.endswith("/v3/account"):
            return 200, {"balances": []}
        raise AssertionError(url)


def test_credentials_are_testnet_only_and_never_repr_secrets(app_config) -> None:
    values = {
        "INVESTMENT_MANAGER_BINANCE_ENVIRONMENT": "testnet",
        "INVESTMENT_MANAGER_BINANCE_API_KEY": "key-value",
        "INVESTMENT_MANAGER_BINANCE_API_SECRET": "secret-value",
        "INVESTMENT_MANAGER_BINANCE_ORDER_SUBMISSION_ENABLED": "true",
    }

    credentials = BinanceCredentials.from_environment(
        app_config.binance_testnet,
        environment=values,
        require_order_submission=True,
    )

    assert "key-value" not in repr(credentials)
    assert "secret-value" not in repr(credentials)
    with pytest.raises(ValueError, match="testnet"):
        BinanceCredentials.from_environment(
            app_config.binance_testnet,
            environment={**values, "INVESTMENT_MANAGER_BINANCE_ENVIRONMENT": "live"},
        )


def test_signed_request_uses_server_time_hmac_and_never_puts_secret_in_url(app_config) -> None:
    transport = FakeTransport()
    credentials = BinanceCredentials(api_key="key-value", api_secret="secret-value")
    client = BinanceTestnetClient(
        app_config.binance_testnet,
        credentials,
        transport=transport,
        wall_clock_ms=lambda: 999_000,
        monotonic_clock=lambda: 1,
    )

    assert client.account() == {"balances": []}

    method, url, headers, timeout = transport.calls[-1]
    parsed = urlparse(url)
    parameters = parse_qs(parsed.query)
    assert method == "GET"
    assert headers == {"X-MBX-APIKEY": "key-value"}
    assert timeout == 10
    assert parameters["timestamp"] == ["1000000"]
    assert parameters["recvWindow"] == ["5000"]
    unsigned = parsed.query.rsplit("&signature=", 1)[0]
    expected = hmac.new(b"secret-value", unsigned.encode(), hashlib.sha256).hexdigest()
    assert parameters["signature"] == [expected]
    assert "secret-value" not in url


def test_symbol_rules_round_down_and_enforce_notional() -> None:
    rules = SymbolRules.from_exchange_info(_exchange_info())

    assert rules.quantity(
        Decimal("0.123456"), market=True, reference_price=Decimal("100")
    ) == Decimal("0.12345")
    assert rules.price(Decimal("100.129")) == Decimal("100.12")
    with pytest.raises(ValueError, match="名义金额"):
        rules.quantity(Decimal("0.00001"), market=True, reference_price=Decimal("100"))
