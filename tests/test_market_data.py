from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.market_data import (
    BinanceMarketStreamService,
    BinanceMessageParser,
    BinancePublicRestClient,
    BinanceWebSocketConnector,
    ClosedMarketBar,
    InMemoryMarketDataStore,
    MarketBootstrapper,
    MarketQuote,
    MarketShockDetector,
    MarketTrade,
    assemble_shadow_market_stream,
)
from investment_manager.market_data_sql import SqlMarketDataStore, create_market_schema

NOW = datetime(2026, 8, 18, 12, 10, tzinfo=UTC)


def _millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _quote(at: datetime = NOW, *, update_id: int = 1) -> MarketQuote:
    return MarketQuote(
        quote_id=f"quote-{update_id}",
        symbol="BTCUSDT",
        observed_at=at,
        bid="100",
        bid_quantity="2",
        ask="100.1",
        ask_quantity="3",
        update_id=update_id,
        source="test",
    )


def _trade(at: datetime = NOW, *, trade_id: int = 4_034_643_345) -> MarketTrade:
    return MarketTrade(
        trade_id=f"trade-{trade_id}",
        symbol="BTCUSDT",
        aggregate_trade_id=trade_id,
        event_time=at - timedelta(milliseconds=10),
        observed_at=at,
        price="100.05",
        quantity="0.1",
        buyer_is_maker=False,
        source="test",
    )


def _bar(open_time: datetime, *, observed_at: datetime = NOW) -> ClosedMarketBar:
    return ClosedMarketBar(
        symbol="BTCUSDT",
        interval="5m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=5) - timedelta(milliseconds=1),
        observed_at=observed_at,
        open="99",
        high="101",
        low="98",
        close="100",
        volume="10",
        source="test",
    )


@pytest.mark.parametrize(
    ("interval", "seconds"),
    (("1m", 60), ("3m", 180), ("5m", 300), ("15m", 900), ("30m", 1800), ("1h", 3600), ("2h", 7200)),
)
def test_market_interval_seconds_are_canonical(app_config, interval, seconds) -> None:
    policy = app_config.market_data.model_copy(update={"interval": interval})

    assert policy.interval_seconds == seconds


def test_official_websocket_contract_parses_quote_trade_and_only_closed_bar() -> None:
    parser = BinanceMessageParser()
    quote = parser.parse(
        json.dumps(
            {
                "stream": "btcusdt@bookTicker",
                "data": {
                    "u": 400900217,
                    "s": "BTCUSDT",
                    "b": "100",
                    "B": "2",
                    "a": "100.1",
                    "A": "3",
                },
            }
        ),
        observed_at=NOW,
    )
    trade = parser.parse(
        json.dumps(
            {
                "stream": "btcusdt@aggTrade",
                "data": {
                    "e": "aggTrade",
                    "E": _millis(NOW),
                    "s": "BTCUSDT",
                    "a": 12345,
                    "p": "100.05",
                    "q": "0.1",
                    "T": _millis(NOW - timedelta(milliseconds=5)),
                    "m": False,
                },
            }
        ),
        observed_at=NOW,
    )
    open_bar = {
        "stream": "btcusdt@kline_5m",
        "data": {
            "e": "kline",
            "E": _millis(NOW),
            "s": "BTCUSDT",
            "k": {
                "t": _millis(NOW - timedelta(minutes=5)),
                "T": _millis(NOW) - 1,
                "i": "5m",
                "o": "99",
                "h": "101",
                "l": "98",
                "c": "100",
                "v": "10",
                "x": False,
            },
        },
    }
    assert isinstance(quote, MarketQuote)
    assert isinstance(trade, MarketTrade)
    assert parser.parse(json.dumps(open_bar), observed_at=NOW) is None
    open_bar["data"]["k"]["x"] = True
    assert isinstance(parser.parse(json.dumps(open_bar), observed_at=NOW), ClosedMarketBar)


class FakeHttpTransport:
    async def get(self, path, params):
        if path.endswith("bookTicker"):
            return {
                "symbol": params["symbol"],
                "bidPrice": "100",
                "bidQty": "2",
                "askPrice": "100.1",
                "askQty": "3",
            }
        if path.endswith("aggTrades"):
            return [
                {
                    "a": 99,
                    "p": "100.05",
                    "q": "0.1",
                    "T": _millis(NOW - timedelta(milliseconds=10)),
                    "m": False,
                }
            ]
        if path.endswith("klines"):
            return [
                [
                    _millis(NOW - timedelta(minutes=10)),
                    "99",
                    "100",
                    "98",
                    "99.5",
                    "10",
                    _millis(NOW - timedelta(minutes=5)) - 1,
                ],
                [
                    _millis(NOW - timedelta(minutes=5)),
                    "99.5",
                    "101",
                    "99",
                    "100",
                    "11",
                    _millis(NOW) - 1,
                ],
                [
                    _millis(NOW),
                    "100",
                    "102",
                    "99",
                    "101",
                    "12",
                    _millis(NOW + timedelta(minutes=5)) - 1,
                ],
            ]
        raise AssertionError(path)


def test_rest_bootstrap_excludes_current_bar_and_builds_visible_snapshot(app_config) -> None:
    async def scenario() -> InMemoryMarketDataStore:
        store = InMemoryMarketDataStore()
        client = BinancePublicRestClient(FakeHttpTransport(), clock=lambda: NOW)
        policy = app_config.market_data.model_copy(update={"symbols": ("BTCUSDT",)})
        await MarketBootstrapper(client, store, policy).refresh()
        return store

    store = asyncio.run(scenario())
    snapshot = store.snapshot(
        cycle_id="shadow-cycle",
        symbol="BTCUSDT",
        interval="5m",
        as_of=NOW,
        bar_window=8,
        source="binance-public-shadow-v1",
    )
    assert len(snapshot.bars) == 2
    assert snapshot.last == Decimal("100.05")
    assert snapshot.source == "binance-public-shadow-v1"


def test_market_shock_detector_filters_ticks_before_creating_trigger(app_config) -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.triggers = []

        def record_trigger(self, trigger):
            self.triggers.append(trigger)
            return True

    sink = RecordingSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=sink,
    )
    first = _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
        update={"price": Decimal("100")}
    )
    quiet = _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
        update={"price": Decimal("101")}
    )
    shock = _trade(NOW + timedelta(seconds=3), trade_id=3).model_copy(
        update={"price": Decimal("102.1")}
    )
    same_window = _trade(NOW + timedelta(seconds=4), trade_id=4).model_copy(
        update={"price": Decimal("104")}
    )

    assert not detector.observe(first)
    assert not detector.observe(quiet)
    assert detector.observe(shock)
    assert not detector.observe(same_window)
    assert len(sink.triggers) == 1
    assert sink.triggers[0].trigger_type.value == "MARKET_SHOCK"
    assert sink.triggers[0].dedup_key.startswith("rolling-trade-v2:300:")


def test_market_shock_closed_bar_is_fallback_not_duplicate(app_config) -> None:
    class RecordingSink:
        def __init__(self):
            self.triggers = []

        def record_trigger(self, trigger):
            self.triggers.append(trigger)
            return True

    sink = RecordingSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=sink,
    )
    first = _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
        update={"price": Decimal("100")}
    )
    shock = _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
        update={"price": Decimal("103")}
    )
    shocked_bar = _bar(
        NOW.replace(second=0, microsecond=0),
        observed_at=NOW + timedelta(minutes=5),
    ).model_copy(update={"open": Decimal("100"), "close": Decimal("100"), "high": Decimal("103")})

    assert not detector.observe(first)
    assert detector.observe(shock)
    assert not detector.observe(shocked_bar)
    assert len(sink.triggers) == 1

    fallback_sink = RecordingSink()
    fallback = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=fallback_sink,
    )
    assert fallback.observe(shocked_bar)
    assert len(fallback_sink.triggers) == 1


def test_market_shock_detects_reversal_and_cross_window_gap(app_config) -> None:
    class RecordingSink:
        def __init__(self):
            self.triggers = []

        def record_trigger(self, trigger):
            self.triggers.append(trigger)
            return True

    reversal_sink = RecordingSink()
    reversal = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=reversal_sink,
    )
    prices = ("100", "101.5", "99")
    results = [
        reversal.observe(
            _trade(NOW + timedelta(seconds=index + 1), trade_id=index + 1).model_copy(
                update={"price": Decimal(price)}
            )
        )
        for index, price in enumerate(prices)
    ]
    assert results == [False, False, True]

    gap_sink = RecordingSink()
    gap = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=gap_sink,
    )
    before_boundary = _trade(NOW + timedelta(minutes=4, seconds=59), trade_id=10).model_copy(
        update={"price": Decimal("100")}
    )
    after_boundary = _trade(NOW + timedelta(minutes=5, seconds=1), trade_id=11).model_copy(
        update={"price": Decimal("103")}
    )
    assert not gap.observe(before_boundary)
    assert gap.observe(after_boundary)
    assert len(gap_sink.triggers) == 1


def test_market_shock_uses_rolling_window_across_fixed_boundaries(app_config) -> None:
    class RecordingSink:
        def __init__(self):
            self.triggers = []

        def record_trigger(self, trigger):
            self.triggers.append(trigger)
            return True

    sink = RecordingSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=600,
        trigger_expiry_seconds=900,
        sink=sink,
    )
    prices = (
        (NOW + timedelta(minutes=4, seconds=59), "100"),
        (NOW + timedelta(minutes=5, seconds=1), "101.1"),
        (NOW + timedelta(minutes=6, seconds=1), "102.1"),
    )

    results = [
        detector.observe(
            _trade(at, trade_id=index).model_copy(update={"price": Decimal(price)})
        )
        for index, (at, price) in enumerate(prices, start=1)
    ]

    assert results == [False, False, True]
    assert len(sink.triggers) == 1


def test_market_shock_rebases_and_cools_down_after_trigger(app_config) -> None:
    class RecordingSink:
        def __init__(self):
            self.triggers = []

        def record_trigger(self, trigger):
            self.triggers.append(trigger)
            return True

    sink = RecordingSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=600,
        trigger_expiry_seconds=900,
        sink=sink,
    )
    samples = (
        (NOW + timedelta(seconds=1), "100"),
        (NOW + timedelta(seconds=2), "102.1"),
        (NOW + timedelta(seconds=3), "102.2"),
        (NOW + timedelta(seconds=4), "104.3"),
        (NOW + timedelta(minutes=10, seconds=3), "104.3"),
        (NOW + timedelta(minutes=10, seconds=4), "106.5"),
    )
    results = [
        detector.observe(
            _trade(at, trade_id=index).model_copy(
                update={"price": Decimal(price)}
            )
        )
        for index, (at, price) in enumerate(samples, start=1)
    ]

    assert results == [False, True, False, False, False, True]
    assert len(sink.triggers) == 2


@pytest.mark.parametrize("backend", ["memory", "sql"])
def test_market_store_is_idempotent_and_never_uses_future_observations(backend) -> None:
    if backend == "memory":
        store = InMemoryMarketDataStore()
    else:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        create_market_schema(engine)
        store = SqlMarketDataStore(engine)
    store.put_quote(_quote())
    store.put_trade(_trade())
    store.put_bar(_bar(NOW - timedelta(minutes=10)))
    store.put_bar(_bar(NOW - timedelta(minutes=5)))
    duplicate = _bar(
        NOW - timedelta(minutes=5),
        observed_at=NOW + timedelta(seconds=1),
    ).model_copy(update={"source": "recovered-rest"})
    assert not store.put_bar(duplicate)
    late_volume_revision = duplicate.model_copy(update={"volume": Decimal("10.1")})
    assert not store.put_bar(late_volume_revision)
    with pytest.raises(ValueError, match="事实不一致"):
        store.put_bar(duplicate.model_copy(update={"high": Decimal("102")}))
    store.put_quote(_quote(NOW + timedelta(minutes=1), update_id=2))
    store.put_trade(_trade(NOW + timedelta(minutes=1), trade_id=2))

    snapshot = store.snapshot(
        cycle_id="visible-cycle",
        symbol="BTCUSDT",
        interval="5m",
        as_of=NOW,
        bar_window=8,
        source="test",
    )
    assert snapshot.bid == Decimal("100")
    assert snapshot.last == Decimal("100.05")
    assert snapshot.observed_at == NOW
    assert snapshot.bars[-1].volume == Decimal("10")


def test_connector_uses_one_combined_public_stream_and_mock_stage_fails_closed(
    app_config,
) -> None:
    uri = BinanceWebSocketConnector(app_config.market_data).uri
    assert uri.startswith("wss://stream.binance.com:9443/stream?streams=")
    assert "btcusdt@bookTicker" in uri
    assert "btcusdt@aggTrade" in uri
    assert "btcusdt@kline_5m" in uri
    with pytest.raises(ValueError, match="SHADOW"):
        assemble_shadow_market_stream(app_config, InMemoryMarketDataStore())


def test_stream_service_bootstraps_and_recovers_after_disconnect(app_config) -> None:
    stop = asyncio.Event()

    class FakeBootstrapper:
        calls = 0

        async def refresh(self):
            self.calls += 1

    class FakeConnector:
        calls = 0

        @asynccontextmanager
        async def open(self):
            self.calls += 1
            current = self.calls

            async def messages():
                if current == 1:
                    yield json.dumps(
                        {
                            "stream": "btcusdt@bookTicker",
                            "data": {
                                "u": 10,
                                "s": "BTCUSDT",
                                "b": "100",
                                "B": "2",
                                "a": "100.1",
                                "A": "3",
                            },
                        }
                    )
                    raise OSError("connection lost")
                stop.set()
                yield json.dumps(
                    {
                        "stream": "btcusdt@aggTrade",
                        "data": {
                            "e": "aggTrade",
                            "s": "BTCUSDT",
                            "a": 11,
                            "p": "100.05",
                            "q": "0.1",
                            "T": _millis(NOW),
                            "m": False,
                        },
                    }
                )

            yield messages()

    async def scenario():
        store = InMemoryMarketDataStore()
        bootstrapper = FakeBootstrapper()
        connector = FakeConnector()
        policy = app_config.market_data.model_copy(
            update={"reconnect_initial_seconds": 0, "reconnect_maximum_seconds": 0}
        )
        service = BinanceMarketStreamService(
            policy=policy,
            bootstrapper=bootstrapper,  # type: ignore[arg-type]
            connector=connector,
            parser=BinanceMessageParser(),
            store=store,
            clock=lambda: NOW,
        )
        await service.run(stop)
        return service, bootstrapper, connector

    service, bootstrapper, connector = asyncio.run(scenario())
    assert bootstrapper.calls == 2
    assert connector.calls == 2
    assert service.health.connect_count == 2
    assert service.health.reconnect_count == 1
    assert service.health.message_count == 2


def test_stream_persists_bounded_samples_but_observes_every_market_event(app_config) -> None:
    class RecordingStore:
        def __init__(self):
            self.quotes = []
            self.trades = []
            self.bars = []

        def put_quote(self, quote):
            self.quotes.append(quote)
            return True

        def put_trade(self, trade):
            self.trades.append(trade)
            return True

        def put_bar(self, bar):
            self.bars.append(bar)
            return True

    observed = []
    store = RecordingStore()
    service = BinanceMarketStreamService(
        policy=app_config.market_data,
        bootstrapper=None,  # type: ignore[arg-type]
        connector=None,  # type: ignore[arg-type]
        parser=BinanceMessageParser(),
        store=store,  # type: ignore[arg-type]
        market_observer=lambda event: observed.append(event) or False,
    )
    events = (
        _quote(NOW, update_id=1),
        _quote(NOW + timedelta(milliseconds=500), update_id=2),
        _quote(NOW + timedelta(seconds=1), update_id=3),
        _trade(NOW, trade_id=1),
        _trade(NOW + timedelta(milliseconds=500), trade_id=2),
        _trade(NOW + timedelta(seconds=1), trade_id=3),
    )

    for event in events:
        service._process_event(event)

    assert len(observed) == 6
    assert [item.update_id for item in store.quotes] == [1, 3]
    assert [item.aggregate_trade_id for item in store.trades] == [1, 3]
    assert service.health.persisted_count == 4
