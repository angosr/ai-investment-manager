from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from quant_core.binance_testnet import (
    BinanceCredentials,
    BinanceManualIntervention,
    BinanceTestnetClient,
    BinanceTestnetExchange,
    BinanceTradingStateSource,
    SymbolRules,
)
from quant_core.cycle import AnalysisCycle
from quant_core.domain import (
    AccountSnapshot,
    ExitReason,
    OrderStatus,
    Position,
    PositionLifecycle,
    PositionLifecycleStatus,
)
from quant_core.ids import stable_id
from quant_core.reconciliation import TradingStateSnapshot


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
        "QUANT_CORE_BINANCE_ENVIRONMENT": "testnet",
        "QUANT_CORE_BINANCE_API_KEY": "key-value",
        "QUANT_CORE_BINANCE_API_SECRET": "secret-value",
        "QUANT_CORE_BINANCE_ORDER_SUBMISSION_ENABLED": "true",
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
            environment={**values, "QUANT_CORE_BINANCE_ENVIRONMENT": "live"},
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


class FakeClient:
    def __init__(self):
        self.orders = {}
        self.new_order_calls = []

    def exchange_info(self, symbol):
        assert symbol == "BTCUSDT"
        return _exchange_info()

    def get_order(self, *, symbol, client_order_id):
        assert symbol == "BTCUSDT"
        return self.orders.get(client_order_id)

    def new_order(self, parameters):
        self.new_order_calls.append(dict(parameters))
        raw = {
            "symbol": parameters["symbol"],
            "orderId": len(self.orders) + 1,
            "clientOrderId": parameters["newClientOrderId"],
            "transactTime": 1_787_055_700_000,
            "status": "FILLED" if parameters["type"] == "MARKET" else "NEW",
            "executedQty": parameters["quantity"] if parameters["type"] == "MARKET" else "0",
            "fills": (
                [
                    {
                        "price": "101.21",
                        "qty": parameters["quantity"],
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "tradeId": 7,
                    }
                ]
                if parameters["type"] == "MARKET"
                else []
            ),
        }
        self.orders[parameters["newClientOrderId"]] = raw
        return raw

    def cancel_order(self, *, symbol, client_order_id):
        raw = {**self.orders[client_order_id], "status": "CANCELED"}
        self.orders[client_order_id] = raw
        return raw

    def trades(self, *, symbol, order_id):
        return []

    def account(self):
        return {
            "canTrade": True,
            "accountType": "SPOT",
            "permissions": ["SPOT"],
            "balances": [
                {"asset": "USDT", "free": "125", "locked": "0"},
                # Spot Testnet may pre-fund base assets. They are not strategy inventory.
                {"asset": "BTC", "free": "5", "locked": "0"},
            ],
        }

    def open_orders(self, symbol=None):
        return []


def test_exchange_queries_before_submit_and_reuses_client_order_id(
    app_config, replay_input
) -> None:
    prepared = AnalysisCycle.create(app_config).prepare(replay_input)
    assert prepared.intent is not None and prepared.risk_decision is not None
    client = FakeClient()
    exchange = BinanceTestnetExchange(client)

    first = exchange.submit(
        intent=prepared.intent,
        risk=prepared.risk_decision,
        market=replay_input.market,
    )
    second = exchange.submit(
        intent=prepared.intent,
        risk=prepared.risk_decision,
        market=replay_input.market,
    )

    assert first == second
    assert first.status == OrderStatus.FILLED
    assert len(client.new_order_calls) == 1


def test_partial_protection_never_submits_duplicate_exit(app_config, replay_input) -> None:
    prepared = AnalysisCycle.create(app_config).prepare(replay_input)
    assert prepared.intent is not None and prepared.risk_decision is not None
    client = FakeClient()
    exchange = BinanceTestnetExchange(client)
    entry = exchange.submit(
        intent=prepared.intent,
        risk=prepared.risk_decision,
        market=replay_input.market,
    )
    fill = entry.fills[0]
    lifecycle = PositionLifecycle(
        position_id="position-1",
        cycle_id=prepared.intent.cycle_id,
        intent_id=prepared.intent.intent_id,
        entry_order_id=entry.order_id,
        reservation_id=prepared.risk_decision.reservation.reservation_id,
        symbol="BTCUSDT",
        quantity=fill.quantity,
        entry_price=fill.price,
        entry_fee=fill.fee,
        stop_price=Decimal("100.50"),
        opened_at=replay_input.market.as_of,
        max_exit_at=replay_input.market.as_of + timedelta(hours=2),
        highest_price=fill.price,
        lowest_price=fill.price,
        status=PositionLifecycleStatus.PROTECTED,
        protection_id="protect-1",
    )
    client.orders["protect-1"] = {
        "orderId": 42,
        "clientOrderId": "protect-1",
        "status": "PARTIALLY_FILLED",
        "executedQty": "0.00001",
        "updateTime": 1_787_055_700_000,
    }

    with pytest.raises(BinanceManualIntervention):
        exchange.submit_position_exit(
            lifecycle=lifecycle,
            market=replay_input.market,
            reason=ExitReason.STOP_LOSS,
        )
    assert len(client.new_order_calls) == 1


class FakeLocalState:
    def __init__(self, positions=()):
        self._positions = positions

    def snapshot(self, *, as_of):
        account = AccountSnapshot(
            cycle_id=stable_id("local", as_of.isoformat()),
            as_of=as_of,
            observed_at=as_of,
            quote_balance=Decimal("100"),
            positions=self._positions,
            reconciled=True,
        )
        return TradingStateSnapshot(
            snapshot_id=stable_id("local_state", as_of.isoformat()),
            as_of=as_of,
            observed_at=as_of,
            account=account,
        )


def test_remote_state_uses_testnet_balance_without_exposing_credentials() -> None:
    client = FakeClient()
    exchange = BinanceTestnetExchange(client)
    source = BinanceTradingStateSource(
        client,
        exchange,
        FakeLocalState(),
        symbols=("BTCUSDT",),
        quote_asset="USDT",
    )
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)

    snapshot = source.snapshot(as_of=now)

    assert snapshot.account.quote_balance == Decimal("125")
    assert snapshot.account.positions == ()
    assert snapshot.orders == ()


def test_remote_state_reports_only_a_shortfall_in_managed_inventory() -> None:
    client = FakeClient()
    client.account = lambda: {
        "canTrade": True,
        "accountType": "SPOT",
        "permissions": ["SPOT"],
        "balances": [
            {"asset": "USDT", "free": "125", "locked": "0"},
            {"asset": "BTC", "free": "0.5", "locked": "0"},
        ],
    }
    local = FakeLocalState(
        positions=(
            Position(
                symbol="BTCUSDT",
                quantity=Decimal("1"),
                average_price=Decimal("100"),
            ),
        )
    )
    source = BinanceTradingStateSource(
        client,
        BinanceTestnetExchange(client),
        local,
        symbols=("BTCUSDT",),
        quote_asset="USDT",
    )

    snapshot = source.snapshot(as_of=datetime(2026, 8, 18, 12, tzinfo=UTC))

    assert snapshot.account.positions[0].quantity == Decimal("0.5")
    assert snapshot.account.positions[0].average_price == Decimal("100")
