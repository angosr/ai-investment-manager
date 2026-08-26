from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.features import (
    build_derivative_context_snapshot,
    freeze_quote_views,
)
from investment_manager.market.models import (
    ClosedMarketBar,
    CrossVenueSpotQuote,
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
    MarketTrade,
    SpotVenue,
    TradFiMarket,
    ValuationQuoteQuality,
)
from investment_manager.market.perpetual.client import BinanceUsdmRestClient
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualProductRules,
    PerpetualQuote,
    TradingScheduleSnapshot,
    TradingSession,
    TradingSessionType,
    perpetual_product_rule_content_hash,
)
from investment_manager.market.perpetual.service import BinancePerpetualMarketService
from investment_manager.market.policy import CrossVenueSpotPolicy, CrossVenueSpotProduct
from investment_manager.market.repository import (
    InMemoryMarketDataStore,
    SqlMarketDataStore,
    create_market_schema,
)
from investment_manager.market.runtime import (
    BinanceMarketStreamService,
    BinanceMessageParser,
    BinancePublicRestClient,
    BinanceWebSocketConnector,
    CoinbaseSpotClient,
    CrossVenueSpotService,
    HttpxPublicJsonTransport,
    KrakenSpotClient,
    MarketBootstrapper,
    MarketShockDetector,
    assemble_shadow_market_stream,
)

NOW = datetime(2026, 8, 18, 12, 10, tzinfo=UTC)


class RecordingTriggerSink:
    def __init__(self) -> None:
        self.triggers = []

    def record_trigger(self, trigger):
        self.triggers.append(trigger)
        return True


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


def _aligned_spot_quote(spot, *, observed_at: datetime = NOW) -> MarketQuote:
    return MarketQuote(
        quote_id=stable_id("aligned_spot_quote", spot.symbol, observed_at),
        symbol=spot.symbol,
        observed_at=observed_at,
        bid=spot.bid,
        bid_quantity="2",
        ask=spot.ask,
        ask_quantity="3",
        source="test",
    )


def _cross_venue_quote(
    venue: SpotVenue,
    *,
    bid: str,
    ask: str,
    observed_at: datetime = NOW,
    marker: str | None = None,
) -> CrossVenueSpotQuote:
    marker = marker or (
        "123"
        if venue == SpotVenue.COINBASE
        else (observed_at - timedelta(milliseconds=1)).isoformat()
    )
    return CrossVenueSpotQuote(
        quote_id=stable_id(
            "cross_venue_spot_quote",
            venue.value,
            "BTCUSDT",
            marker,
        ),
        venue=venue,
        symbol="BTCUSDT",
        provider_symbol=("BTC-USDT" if venue == SpotVenue.COINBASE else "BTC/USDT"),
        exchange_time=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        bid=bid,
        bid_quantity="1",
        ask=ask,
        ask_quantity="2",
        source_sequence=(marker if venue == SpotVenue.COINBASE else None),
        source=(
            "coinbase-exchange-rest"
            if venue == SpotVenue.COINBASE
            else "kraken-spot-rest"
        ),
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


def _perpetual_instrument() -> InstrumentId:
    return InstrumentId(
        product=InstrumentProduct.USD_M_PERPETUAL,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDT",
    )


def _tradfi_instrument() -> InstrumentId:
    return InstrumentId(
        product=InstrumentProduct.TRADFI_PERPETUAL,
        symbol="SPYUSDT",
        base_asset="SPY",
        quote_asset="USDT",
        settlement_asset="USDT",
        tradfi_market=TradFiMarket.EQUITY,
    )


def _trading_schedule(*, observed_at: datetime = NOW) -> TradingScheduleSnapshot:
    exchange_time = observed_at - timedelta(seconds=1)
    return TradingScheduleSnapshot(
        schedule_id=stable_id(
            "tradfi_trading_schedule",
            exchange_time.isoformat(),
        ),
        exchange_time=exchange_time,
        observed_at=observed_at,
        sessions=(
            TradingSession(
                market=TradFiMarket.EQUITY,
                starts_at=NOW - timedelta(hours=2),
                ends_at=NOW + timedelta(hours=2),
                session_type=TradingSessionType.REGULAR,
            ),
        ),
        source="test",
    )


def _perpetual_state(
    *,
    observed_at: datetime = NOW,
    instrument: InstrumentId | None = None,
) -> PerpetualMarketState:
    instrument = instrument or _perpetual_instrument()
    exchange_time = observed_at - timedelta(seconds=1)
    return PerpetualMarketState(
        state_id=stable_id(
            "perpetual_market_state",
            instrument.key,
            exchange_time.isoformat(),
        ),
        instrument=instrument,
        exchange_time=exchange_time,
        observed_at=observed_at,
        mark_price="100.2",
        index_price="100",
        estimated_settle_price="100.1",
        last_funding_rate="0.0001",
        interest_rate="0.0001",
        next_funding_time=observed_at + timedelta(hours=4),
        positioning_observed_at=observed_at - timedelta(minutes=5),
        positioning_window_minutes=60,
        open_interest="110",
        open_interest_value="11000",
        open_interest_change_fraction="0.10",
        global_long_short_account_ratio="1.5",
        global_long_account_fraction="0.6",
        global_short_account_fraction="0.4",
        taker_buy_sell_ratio="1.25",
        taker_buy_volume="500",
        taker_sell_volume="400",
        source="test",
    )


def _perpetual_quote(
    *,
    observed_at: datetime = NOW,
    update_id: int | None = 42,
    instrument: InstrumentId | None = None,
) -> PerpetualQuote:
    instrument = instrument or _perpetual_instrument()
    exchange_time = observed_at - timedelta(seconds=1)
    marker: str | int = update_id if update_id is not None else exchange_time.isoformat()
    return PerpetualQuote(
        quote_id=stable_id("perpetual_quote", instrument.key, marker),
        instrument=instrument,
        exchange_time=exchange_time,
        observed_at=observed_at,
        bid="100",
        bid_quantity="2",
        ask="100.1",
        ask_quantity="3",
        update_id=update_id,
        source="test",
    )


def _perpetual_product_rules(
    *,
    observed_at: datetime = NOW,
    instrument: InstrumentId | None = None,
    upstream_updated_at: datetime | None = None,
) -> PerpetualProductRules:
    instrument = instrument or _perpetual_instrument()
    values = {
        "instrument": instrument,
        "observed_at": observed_at,
        "upstream_updated_at": (
            upstream_updated_at
            if upstream_updated_at is not None
            else observed_at - timedelta(seconds=1)
        ),
        "contract_type": (
            "TRADIFI_PERPETUAL"
            if instrument.product == InstrumentProduct.TRADFI_PERPETUAL
            else "PERPETUAL"
        ),
        "status": "TRADING",
        "margin_asset": instrument.settlement_asset,
        "tick_size": Decimal("0.1"),
        "market_min_quantity": Decimal("0.001"),
        "market_max_quantity": Decimal("120"),
        "market_quantity_step": Decimal("0.001"),
        "minimum_notional": Decimal("50"),
        "funding_override_present": True,
        "funding_interval_hours": 8,
        "adjusted_funding_rate_cap": Decimal("0.003"),
        "adjusted_funding_rate_floor": Decimal("-0.003"),
        "source": "test",
    }
    pending = PerpetualProductRules.model_construct(rules_id="pending", **values)
    return PerpetualProductRules(
        rules_id=stable_id(
            "perpetual_product_rules",
            instrument.key,
            observed_at.isoformat(),
            perpetual_product_rule_content_hash(pending),
        ),
        **values,
    )


def _funding_settlement(
    *,
    observed_at: datetime = NOW,
    instrument: InstrumentId | None = None,
) -> FundingSettlement:
    instrument = instrument or _perpetual_instrument()
    funding_time = NOW - timedelta(hours=4)
    return FundingSettlement(
        settlement_id=stable_id(
            "funding_settlement",
            instrument.key,
            funding_time.isoformat(),
            FundingRateType.REGULAR.value,
        ),
        instrument=instrument,
        funding_time=funding_time,
        observed_at=observed_at,
        funding_rate="0.0001",
        mark_price="99.8",
        rate_type=FundingRateType.REGULAR,
        source="test",
    )


def test_quote_views_separate_valuation_from_execution_authority() -> None:
    spot = InstrumentId.binance_spot(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    spot_valuation, spot_executable = freeze_quote_views(
        instrument=spot,
        quote=_quote(),
        as_of=NOW,
        maximum_live_age_seconds=5,
    )
    assert spot_valuation.quality == ValuationQuoteQuality.LIVE_MARKET
    assert spot_executable is not None
    assert spot_executable.source_quote_id == spot_valuation.source_quote_id

    tradfi = _tradfi_instrument()
    schedule = _trading_schedule()
    closed_schedule = schedule.model_copy(
        update={
            "sessions": (
                schedule.sessions[0].model_copy(
                    update={"session_type": TradingSessionType.NO_TRADING}
                ),
            )
        }
    )
    closed_valuation, closed_executable = freeze_quote_views(
        instrument=tradfi,
        quote=_perpetual_quote(instrument=tradfi),
        as_of=NOW,
        maximum_live_age_seconds=5,
        trading_schedule=closed_schedule,
    )
    assert closed_valuation.quality == ValuationQuoteQuality.CLOSED_MARKET
    assert closed_valuation.trading_schedule_ref == schedule.schedule_id
    assert closed_executable is None

    live_valuation, live_executable = freeze_quote_views(
        instrument=tradfi,
        quote=_perpetual_quote(instrument=tradfi),
        as_of=NOW,
        maximum_live_age_seconds=5,
        trading_schedule=schedule,
    )
    assert live_valuation.quality == ValuationQuoteQuality.LIVE_MARKET
    assert live_executable is not None
    assert live_executable.trading_schedule_ref == schedule.schedule_id

    stale_valuation, stale_executable = freeze_quote_views(
        instrument=tradfi,
        quote=_perpetual_quote(instrument=tradfi),
        as_of=NOW + timedelta(seconds=6),
        maximum_live_age_seconds=5,
        trading_schedule=schedule,
    )
    assert stale_valuation.quality == ValuationQuoteQuality.STALE_MARKET
    assert stale_valuation.trading_schedule_ref == schedule.schedule_id
    assert stale_executable is None

    with pytest.raises(ValueError, match=r"交易日历.*尚不可见"):
        freeze_quote_views(
            instrument=tradfi,
            quote=_perpetual_quote(instrument=tradfi),
            as_of=NOW,
            maximum_live_age_seconds=5,
            trading_schedule=schedule.model_copy(
                update={"observed_at": NOW + timedelta(seconds=1)}
            ),
        )


@pytest.mark.parametrize(
    ("interval", "seconds"),
    (("1m", 60), ("3m", 180), ("5m", 300), ("15m", 900), ("30m", 1800), ("1h", 3600), ("2h", 7200)),
)
def test_market_interval_seconds_are_canonical(app_config, interval, seconds) -> None:
    policy = app_config.market_data.model_copy(update={"interval": interval})

    assert policy.interval_seconds == seconds


def test_tradfi_instrument_requires_the_official_market_calendar() -> None:
    with pytest.raises(ValueError, match="官方交易日历"):
        InstrumentId(
            product=InstrumentProduct.TRADFI_PERPETUAL,
            symbol="SPYUSDT",
            base_asset="SPY",
            quote_asset="USDT",
            settlement_asset="USDT",
        )


def test_derivative_context_is_dense_point_in_time_evidence(replay_input) -> None:
    bars = tuple(
        item.model_copy(
            update={
                "quote_volume": item.volume * item.close,
                "taker_buy_base_volume": item.volume * Decimal("0.60"),
                "taker_buy_quote_volume": item.volume * item.close * Decimal("0.60"),
            }
        )
        for item in replay_input.market.bars
    )
    spot = replay_input.market.model_copy(
        update={
            "cycle_id": "analysis-1",
            "as_of": NOW,
            "observed_at": NOW,
            "bars": bars,
        }
    )
    settlement = _funding_settlement(observed_at=NOW - timedelta(seconds=1))

    snapshot = build_derivative_context_snapshot(
        cycle_id="analysis-1",
        asset="BTC",
        spot=spot,
        aligned_spot_quote=_aligned_spot_quote(spot),
        state=_perpetual_state(observed_at=NOW),
        quote=_perpetual_quote(observed_at=NOW),
        settlements=(settlement,),
        funding_window_hours=24,
        maximum_quote_skew_seconds=15,
    )

    assert snapshot.mark_index_premium_bps == Decimal("20")
    assert snapshot.executable_short_basis_bps == (
        Decimal("100") / spot.ask - Decimal("1")
    ) * Decimal("10000")
    assert snapshot.last_funding_rate_bps == Decimal("1")
    assert snapshot.trailing_funding_rate_sum_bps == Decimal("1")
    assert snapshot.trailing_funding_rate_mean_bps == Decimal("1")
    assert snapshot.trailing_funding_rate_stddev_bps == Decimal("0")
    assert snapshot.trailing_funding_positive_fraction == Decimal("1")
    assert snapshot.trailing_funding_rate_min_bps == Decimal("1")
    assert snapshot.funding_settlement_count == 1
    assert snapshot.positioning_observed_at == NOW - timedelta(minutes=5)
    assert snapshot.positioning_window_minutes == 60
    assert snapshot.open_interest == Decimal("110")
    assert snapshot.open_interest_change_fraction == Decimal("0.10")
    assert snapshot.global_long_short_account_ratio == Decimal("1.5")
    assert snapshot.taker_buy_sell_ratio == Decimal("1.25")
    assert snapshot.spot_flow_window_minutes == 40
    assert snapshot.spot_taker_buy_sell_ratio == Decimal("1.5")
    assert snapshot.input_refs == tuple(
        sorted(
            (
                content_hash(spot),
                _aligned_spot_quote(spot).quote_id,
                _perpetual_state(observed_at=NOW).state_id,
                _perpetual_quote(observed_at=NOW).quote_id,
                settlement.settlement_id,
            )
        )
    )


def test_derivative_context_compresses_fresh_independent_spot_venues(replay_input) -> None:
    spot = replay_input.market.model_copy(
        update={"cycle_id": "cross-venue", "as_of": NOW, "observed_at": NOW}
    )
    external = (
        _cross_venue_quote(SpotVenue.COINBASE, bid="99.8", ask="100.0"),
        _cross_venue_quote(SpotVenue.KRAKEN, bid="100.2", ask="100.3"),
    )

    snapshot = build_derivative_context_snapshot(
        cycle_id="cross-venue",
        asset="BTC",
        spot=spot,
        aligned_spot_quote=_aligned_spot_quote(spot),
        state=_perpetual_state(observed_at=NOW),
        quote=_perpetual_quote(observed_at=NOW),
        settlements=(),
        funding_window_hours=24,
        maximum_quote_skew_seconds=15,
        cross_venue_quotes=external,
        maximum_cross_venue_age_seconds=30,
    )

    assert snapshot.spot_venue_count == 3
    assert snapshot.cross_venue_observed_at == NOW
    assert snapshot.spot_mid_range_bps is not None
    assert snapshot.spot_mid_range_bps > 0
    assert snapshot.reference_spot_mid_deviation_bps is not None
    assert snapshot.widest_spot_spread_bps is not None
    assert {item.quote_id for item in external}.issubset(snapshot.input_refs)

    stale = tuple(
        item.model_copy(
            update={
                "exchange_time": NOW - timedelta(seconds=31, milliseconds=1),
                "observed_at": NOW - timedelta(seconds=31),
            }
        )
        for item in external
    )
    with pytest.raises(ValueError, match="已过期"):
        build_derivative_context_snapshot(
            cycle_id="cross-venue",
            asset="BTC",
            spot=spot,
            aligned_spot_quote=_aligned_spot_quote(spot),
            state=_perpetual_state(observed_at=NOW),
            quote=_perpetual_quote(observed_at=NOW),
            settlements=(),
            funding_window_hours=24,
            maximum_quote_skew_seconds=15,
            cross_venue_quotes=stale,
            maximum_cross_venue_age_seconds=30,
        )


def test_derivative_basis_uses_time_aligned_spot_quote_not_latest_snapshot(
    replay_input,
) -> None:
    spot = replay_input.market.model_copy(
        update={"cycle_id": "analysis-aligned", "as_of": NOW, "observed_at": NOW}
    )
    aligned = _aligned_spot_quote(
        spot,
        observed_at=NOW - timedelta(seconds=5),
    ).model_copy(
        update={"bid": spot.bid + Decimal("10"), "ask": spot.ask + Decimal("10")}
    )
    perpetual = _perpetual_quote(observed_at=NOW)

    snapshot = build_derivative_context_snapshot(
        cycle_id="analysis-aligned",
        asset="BTC",
        spot=spot,
        aligned_spot_quote=aligned,
        state=_perpetual_state(observed_at=NOW),
        quote=perpetual,
        settlements=(),
        funding_window_hours=24,
        maximum_quote_skew_seconds=15,
    )

    assert snapshot.executable_short_basis_bps == (
        perpetual.bid / aligned.ask - Decimal("1")
    ) * Decimal("10000")
    assert snapshot.executable_short_basis_bps != (
        perpetual.bid / spot.ask - Decimal("1")
    ) * Decimal("10000")


def test_derivative_basis_rejects_misaligned_cross_market_quote(replay_input) -> None:
    spot = replay_input.market.model_copy(
        update={"cycle_id": "analysis-misaligned", "as_of": NOW, "observed_at": NOW}
    )

    with pytest.raises(ValueError, match="报价时间偏差过大"):
        build_derivative_context_snapshot(
            cycle_id="analysis-misaligned",
            asset="BTC",
            spot=spot,
            aligned_spot_quote=_aligned_spot_quote(
                spot,
                observed_at=NOW - timedelta(seconds=16),
            ),
            state=_perpetual_state(observed_at=NOW),
            quote=_perpetual_quote(observed_at=NOW),
            settlements=(),
            funding_window_hours=24,
            maximum_quote_skew_seconds=15,
        )
def test_derivative_context_without_visible_funding_keeps_empty_summary(
    replay_input,
) -> None:
    spot = replay_input.market.model_copy(
        update={"cycle_id": "analysis-1", "as_of": NOW, "observed_at": NOW}
    )
    too_old = _funding_settlement(observed_at=NOW - timedelta(seconds=1)).model_copy(
        update={
            "settlement_id": stable_id(
                "funding_settlement",
                _perpetual_instrument().key,
                (NOW - timedelta(hours=25)).isoformat(),
                FundingRateType.REGULAR.value,
            ),
            "funding_time": NOW - timedelta(hours=25),
        }
    )

    snapshot = build_derivative_context_snapshot(
        cycle_id="analysis-1",
        asset="BTC",
        spot=spot,
        aligned_spot_quote=_aligned_spot_quote(spot),
        state=_perpetual_state(observed_at=NOW),
        quote=_perpetual_quote(observed_at=NOW),
        settlements=(too_old,),
        funding_window_hours=24,
        maximum_quote_skew_seconds=15,
    )

    assert snapshot.funding_settlement_count == 0
    assert snapshot.trailing_funding_rate_sum_bps is None
    assert snapshot.trailing_funding_rate_mean_bps is None
    assert snapshot.trailing_funding_rate_stddev_bps is None
    assert snapshot.trailing_funding_positive_fraction is None
    assert snapshot.trailing_funding_rate_min_bps is None
    assert len(snapshot.input_refs) == 4


def test_spot_flow_reports_actual_contiguous_cold_start_window(replay_input) -> None:
    enriched = tuple(
        item.model_copy(
            update={
                "quote_volume": item.volume * item.close,
                "taker_buy_base_volume": item.volume * Decimal("0.60"),
                "taker_buy_quote_volume": item.volume * item.close * Decimal("0.60"),
            }
        )
        for item in replay_input.market.bars[-2:]
    )
    spot = replay_input.market.model_copy(
        update={
            "cycle_id": "analysis-cold-start",
            "as_of": NOW,
            "observed_at": NOW,
            "bars": (*replay_input.market.bars[:-2], *enriched),
        }
    )

    snapshot = build_derivative_context_snapshot(
        cycle_id="analysis-cold-start",
        asset="BTC",
        spot=spot,
        aligned_spot_quote=_aligned_spot_quote(spot),
        state=_perpetual_state(observed_at=NOW),
        quote=_perpetual_quote(observed_at=NOW),
        settlements=(),
        funding_window_hours=24,
        maximum_quote_skew_seconds=15,
    )

    assert snapshot.spot_flow_window_minutes == 10
    assert snapshot.spot_taker_buy_sell_ratio == Decimal("1.5")


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
                "q": "1000",
                "V": "6",
                "Q": "600",
                "x": False,
            },
        },
    }
    assert isinstance(quote, MarketQuote)
    assert isinstance(trade, MarketTrade)
    assert parser.parse(json.dumps(open_bar), observed_at=NOW) is None
    open_bar["data"]["k"]["x"] = True
    closed_bar = parser.parse(json.dumps(open_bar), observed_at=NOW)
    assert isinstance(closed_bar, ClosedMarketBar)
    assert closed_bar.taker_buy_base_volume == Decimal("6")


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
                    "995",
                    20,
                    "6",
                    "597",
                    "0",
                ],
                [
                    _millis(NOW - timedelta(minutes=5)),
                    "99.5",
                    "101",
                    "99",
                    "100",
                    "11",
                    _millis(NOW) - 1,
                    "1100",
                    22,
                    "7",
                    "700",
                    "0",
                ],
                [
                    _millis(NOW),
                    "100",
                    "102",
                    "99",
                    "101",
                    "12",
                    _millis(NOW + timedelta(minutes=5)) - 1,
                    "1212",
                    24,
                    "8",
                    "808",
                    "0",
                ],
            ]
        raise AssertionError(path)


def test_cross_venue_clients_parse_first_party_l1_books_and_store_point_in_time() -> None:
    product = CrossVenueSpotProduct(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        coinbase_product_id="BTC-USDT",
        kraken_pair="BTC/USDT",
    )

    class CoinbaseTransport:
        async def get(self, path, params):
            assert path == "/products/BTC-USDT/book"
            assert params == {"level": 1}
            return {
                "bids": [["99.9", "2", 1]],
                "asks": [["100.1", "3", 1]],
                "sequence": 123,
                "time": (NOW - timedelta(milliseconds=2)).isoformat(),
            }

    class KrakenTransport:
        async def get(self, path, params):
            assert path == "/0/public/Depth"
            assert params == {"pair": "BTC/USDT", "count": 1, "assetVersion": 1}
            exchange_seconds = Decimal(str((NOW - timedelta(milliseconds=1)).timestamp()))
            return {
                "error": [],
                "result": {
                    "BTC/USDT": {
                        "bids": [["99.8", "4", str(exchange_seconds)]],
                        "asks": [["100.2", "5", str(exchange_seconds)]],
                    }
                },
            }

    async def scenario():
        return await asyncio.gather(
            CoinbaseSpotClient(CoinbaseTransport(), clock=lambda: NOW).fetch(product),
            KrakenSpotClient(KrakenTransport(), clock=lambda: NOW).fetch(product),
        )

    coinbase, kraken = asyncio.run(scenario())
    assert coinbase.venue == SpotVenue.COINBASE
    assert coinbase.source_sequence == "123"
    assert kraken.venue == SpotVenue.KRAKEN
    assert kraken.exchange_time == NOW - timedelta(milliseconds=1)

    store = InMemoryMarketDataStore()
    assert store.put_cross_venue_spot_quote(coinbase)
    assert store.put_cross_venue_spot_quote(kraken)
    assert store.latest_cross_venue_spot_quotes(
        symbol="BTCUSDT",
        venues=(SpotVenue.COINBASE, SpotVenue.KRAKEN),
        as_of=NOW,
    ) == (coinbase, kraken)


def test_cross_venue_service_refreshes_each_source_independently_without_ai_trigger() -> None:
    products = (
        CrossVenueSpotProduct(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            coinbase_product_id="BTC-USDT",
            kraken_pair="BTC/USDT",
        ),
        CrossVenueSpotProduct(
            symbol="ETHUSDT",
            base_asset="ETH",
            quote_asset="USDT",
            coinbase_product_id="ETH-USDT",
            kraken_pair="ETH/USDT",
        ),
    )

    class CoinbaseTransport:
        async def get(self, path, params):
            symbol = path.split("/")[2].replace("-", "")
            assert params == {"level": 1}
            return {
                "bids": [["99.9", "2", 1]],
                "asks": [["100.1", "3", 1]],
                "sequence": 123 if symbol == "BTCUSDT" else 456,
                "time": (NOW - timedelta(milliseconds=2)).isoformat(),
            }

    class KrakenTransport:
        async def get(self, path, params):
            assert path == "/0/public/Depth"
            pair = params["pair"]
            exchange_seconds = str((NOW - timedelta(milliseconds=1)).timestamp())
            return {
                "error": [],
                "result": {
                    pair: {
                        "bids": [["99.8", "4", exchange_seconds]],
                        "asks": [["100.2", "5", exchange_seconds]],
                    }
                },
            }

    async def scenario():
        store = InMemoryMarketDataStore()
        stop = asyncio.Event()
        refreshes = []

        def observe(result):
            refreshes.append(result)
            if len(refreshes) == 2:
                stop.set()

        service = CrossVenueSpotService(
            policy=CrossVenueSpotPolicy(
                version="cross-venue-spot-test-v1",
                products=products,
                poll_seconds=10,
                maximum_age_seconds=30,
            ),
            coinbase=CoinbaseSpotClient(CoinbaseTransport(), clock=lambda: NOW),
            kraken=KrakenSpotClient(KrakenTransport(), clock=lambda: NOW),
            store=store,
            refresh_observer=observe,
            clock=lambda: NOW,
        )
        await service.run(stop)
        return store, service, tuple(refreshes)

    store, service, refreshes = asyncio.run(scenario())
    assert tuple(sorted(item.venue for item in refreshes)) == (
        SpotVenue.COINBASE,
        SpotVenue.KRAKEN,
    )
    assert all(item.succeeded and item.observation_count == 2 for item in refreshes)
    assert service.health.quote_count == 4
    assert len(
        store.latest_cross_venue_spot_quotes(
            symbol="ETHUSDT",
            venues=(SpotVenue.COINBASE, SpotVenue.KRAKEN),
            as_of=NOW,
        )
    ) == 2


def test_http_transport_reuses_and_closes_one_connection_pool(monkeypatch) -> None:
    clients = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.calls = []
            clients.append(self)

        async def get(self, path, params):
            self.calls.append((path, params))
            return FakeResponse()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(
        "investment_manager.market.runtime.httpx.AsyncClient",
        FakeAsyncClient,
    )

    async def scenario():
        transport = HttpxPublicJsonTransport("https://api.binance.com", 10)
        assert await transport.get("/one", {"symbol": "BTCUSDT"}) == {"ok": True}
        assert await transport.get("/two", {"symbol": "ETHUSDT"}) == {"ok": True}
        await transport.aclose()

    asyncio.run(scenario())
    assert len(clients) == 1
    assert len(clients[0].calls) == 2
    assert clients[0].closed


class FakePerpetualTransport:
    def __init__(
        self,
        *,
        reverse_funding: bool = False,
        funding_override: bool = True,
    ) -> None:
        self.reverse_funding = reverse_funding
        self.funding_override = funding_override

    async def get(self, path, params):
        if path.endswith("exchangeInfo"):
            assert params == {}
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "marginAsset": "USDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0.001",
                                "maxQty": "120",
                                "stepSize": "0.001",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "50"},
                        ],
                    }
                ]
            }
        if path.endswith("fundingInfo"):
            assert params == {}
            if not self.funding_override:
                return []
            return [
                {
                    "symbol": "BTCUSDT",
                    "adjustedFundingRateCap": "0.003",
                    "adjustedFundingRateFloor": "-0.003",
                    "fundingIntervalHours": 8,
                    "updateTime": _millis(NOW - timedelta(seconds=1)),
                }
            ]
        if path.endswith("tradingSchedule"):
            assert params == {}
            return {
                "updateTime": _millis(NOW - timedelta(seconds=1)),
                "marketSchedules": {
                    "EQUITY": {
                        "sessions": [
                            {
                                "startTime": _millis(NOW - timedelta(hours=2)),
                                "endTime": _millis(NOW + timedelta(hours=2)),
                                "type": "REGULAR",
                            }
                        ]
                    }
                },
            }
        if path.endswith("bookTicker"):
            return {
                "symbol": params["symbol"],
                "bidPrice": "100",
                "bidQty": "2",
                "askPrice": "100.1",
                "askQty": "3",
                "time": _millis(NOW - timedelta(seconds=1)),
                "lastUpdateId": 42,
            }
        if path.endswith("premiumIndex"):
            return {
                "symbol": params["symbol"],
                "markPrice": "100.2",
                "indexPrice": "100",
                "estimatedSettlePrice": "100.1",
                "lastFundingRate": "0.0001",
                "interestRate": "0.0001",
                "nextFundingTime": _millis(NOW + timedelta(hours=4)),
                "time": _millis(NOW - timedelta(seconds=1)),
            }
        if path.endswith("openInterestHist"):
            return [
                {
                    "symbol": params["symbol"],
                    "sumOpenInterest": "100" if index < 12 else "110",
                    "sumOpenInterestValue": "10000" if index < 12 else "11000",
                    "timestamp": _millis(NOW - timedelta(minutes=65 - index * 5)),
                }
                for index in range(13)
            ]
        if path.endswith("globalLongShortAccountRatio"):
            return [
                {
                    "symbol": params["symbol"],
                    "longShortRatio": "1.5",
                    "longAccount": "0.6",
                    "shortAccount": "0.4",
                    "timestamp": _millis(NOW - timedelta(minutes=5)),
                }
            ]
        if path.endswith("takerlongshortRatio"):
            return [
                {
                    "buySellRatio": "1.25",
                    "buyVol": "50",
                    "sellVol": "40",
                    "timestamp": _millis(NOW - timedelta(minutes=50 - index * 5)),
                }
                for index in range(10)
            ]
        if path.endswith("fundingRate"):
            values = [
                {
                    "symbol": params["symbol"],
                    "fundingTime": _millis(NOW - timedelta(hours=8)),
                    "fundingRate": "0.0002",
                    "markPrice": "99.1",
                    "rateType": "Regular",
                },
                {
                    "symbol": params["symbol"],
                    "fundingTime": _millis(NOW - timedelta(hours=4)),
                    "fundingRate": "0.0001",
                    "markPrice": "99.8",
                    "rateType": "Regular",
                },
            ]
            return list(reversed(values)) if self.reverse_funding else values
        raise AssertionError(path)


def test_usdm_rest_client_preserves_exchange_and_observation_time() -> None:
    async def scenario():
        client = BinanceUsdmRestClient(FakePerpetualTransport(), clock=lambda: NOW)
        rules = await client.fetch_product_rules((_perpetual_instrument(),))
        schedule = await client.fetch_trading_schedule()
        quote = await client.fetch_quote(_perpetual_instrument())
        state = await client.fetch_market_state(_perpetual_instrument())
        settlements = await client.fetch_funding_settlements(
            _perpetual_instrument(),
            start=NOW - timedelta(hours=12),
            end=NOW,
        )
        return rules, schedule, quote, state, settlements

    rules, schedule, quote, state, settlements = asyncio.run(scenario())
    assert len(rules) == 1
    assert rules[0].market_quantity_step == Decimal("0.001")
    assert rules[0].minimum_notional == Decimal("50")
    assert rules[0].funding_override_present
    assert rules[0].funding_interval_hours == 8
    assert schedule.model_dump(exclude={"source"}) == _trading_schedule().model_dump(
        exclude={"source"}
    )
    assert schedule.source == "binance-usdm-trading-schedule-rest"
    assert schedule.session_at(instrument=_tradfi_instrument(), at=NOW) is not None
    assert quote.model_dump(exclude={"source"}) == _perpetual_quote().model_dump(exclude={"source"})
    assert quote.source == "binance-usdm-book-ticker-rest"
    assert state.exchange_time == NOW - timedelta(seconds=1)
    assert state.observed_at == NOW
    assert state.premium_fraction == Decimal("0.002")
    assert state.positioning_observed_at == NOW - timedelta(minutes=5)
    assert state.positioning_window_minutes == 60
    assert state.open_interest == Decimal("110")
    assert state.open_interest_value == Decimal("11000")
    assert state.open_interest_change_fraction == Decimal("0.10")
    assert state.global_long_short_account_ratio == Decimal("1.5")
    assert state.global_long_account_fraction == Decimal("0.6")
    assert state.global_short_account_fraction == Decimal("0.4")
    assert state.taker_buy_sell_ratio == Decimal("1.25")
    assert state.taker_buy_volume == Decimal("500")
    assert state.taker_sell_volume == Decimal("400")
    assert [item.funding_time for item in settlements] == [
        NOW - timedelta(hours=8),
        NOW - timedelta(hours=4),
    ]
    assert all(item.rate_type == FundingRateType.REGULAR for item in settlements)


def test_usdm_rest_client_treats_funding_info_as_sparse_override() -> None:
    rules = asyncio.run(
        BinanceUsdmRestClient(
            FakePerpetualTransport(funding_override=False),
            clock=lambda: NOW,
        ).fetch_product_rules((_perpetual_instrument(),))
    )

    assert len(rules) == 1
    assert rules[0].status == "TRADING"
    assert not rules[0].funding_override_present
    assert rules[0].funding_interval_hours is None
    assert rules[0].adjusted_funding_rate_cap is None
    assert rules[0].adjusted_funding_rate_floor is None


def test_usdm_rest_client_rejects_noncanonical_funding_history() -> None:
    async def scenario() -> None:
        client = BinanceUsdmRestClient(
            FakePerpetualTransport(reverse_funding=True),
            clock=lambda: NOW,
        )
        await client.fetch_funding_settlements(
            _perpetual_instrument(),
            start=NOW - timedelta(hours=12),
            end=NOW,
        )

    with pytest.raises(ValueError, match="唯一且升序"):
        asyncio.run(scenario())


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
    sink = RecordingTriggerSink()
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


def test_market_shock_routes_to_portfolio_owner_without_losing_subject(app_config) -> None:
    sink = RecordingTriggerSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=sink,
        analysis_owner_symbol="BTCUSDT",
    )
    first = _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
        update={"symbol": "ETHUSDT", "price": Decimal("100")}
    )
    shock = _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
        update={"symbol": "ETHUSDT", "price": Decimal("103")}
    )

    assert not detector.observe(first)
    assert detector.observe(shock)
    assert len(sink.triggers) == 1
    assert sink.triggers[0].symbol == "BTCUSDT"
    assert sink.triggers[0].affected_symbols == ("ETHUSDT",)


def test_market_shock_publishes_one_ordinary_event_per_portfolio_episode(
    app_config,
) -> None:
    sink = RecordingTriggerSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.01"),
        window_seconds=600,
        trigger_expiry_seconds=900,
        sink=sink,
        analysis_owner_symbol="BTCUSDT",
        trigger_symbols=("BTCUSDT", "ETHUSDT"),
    )
    observations = (
        _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
            update={"symbol": "BTCUSDT", "price": Decimal("100")}
        ),
        _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
            update={"symbol": "ETHUSDT", "price": Decimal("100")}
        ),
        _trade(NOW + timedelta(seconds=3), trade_id=3).model_copy(
            update={"symbol": "BTCUSDT", "price": Decimal("101.01")}
        ),
        _trade(NOW + timedelta(seconds=4), trade_id=4).model_copy(
            update={"symbol": "ETHUSDT", "price": Decimal("101.01")}
        ),
    )

    assert [detector.observe(item) for item in observations] == [False, False, True, False]
    assert len(sink.triggers) == 1
    assert sink.triggers[0].priority < 100
    assert sink.triggers[0].affected_symbols == ("BTCUSDT",)


def test_market_shock_allows_one_urgent_escalation_in_portfolio_episode(
    app_config,
) -> None:
    sink = RecordingTriggerSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.01"),
        window_seconds=600,
        trigger_expiry_seconds=900,
        sink=sink,
        analysis_owner_symbol="BTCUSDT",
        trigger_symbols=("BTCUSDT", "ETHUSDT"),
    )
    observations = (
        _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
            update={"symbol": "BTCUSDT", "price": Decimal("100")}
        ),
        _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
            update={"symbol": "ETHUSDT", "price": Decimal("100")}
        ),
        _trade(NOW + timedelta(seconds=3), trade_id=3).model_copy(
            update={"symbol": "BTCUSDT", "price": Decimal("101.01")}
        ),
        _trade(NOW + timedelta(seconds=4), trade_id=4).model_copy(
            update={"symbol": "ETHUSDT", "price": Decimal("101.30")}
        ),
        _trade(NOW + timedelta(seconds=5), trade_id=5).model_copy(
            update={"symbol": "ETHUSDT", "price": Decimal("102.60")}
        ),
    )

    assert [detector.observe(item) for item in observations] == [
        False,
        False,
        True,
        True,
        False,
    ]
    assert [item.priority for item in sink.triggers] == [80, 100]
    assert sink.triggers[1].affected_symbols == ("ETHUSDT",)


def test_market_shock_starts_new_portfolio_episode_after_window(app_config) -> None:
    sink = RecordingTriggerSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.01"),
        window_seconds=600,
        trigger_expiry_seconds=900,
        sink=sink,
        analysis_owner_symbol="BTCUSDT",
        trigger_symbols=("BTCUSDT", "ETHUSDT"),
    )
    first = _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
        update={"symbol": "BTCUSDT", "price": Decimal("100")}
    )
    first_shock = _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
        update={"symbol": "BTCUSDT", "price": Decimal("101.01")}
    )
    next_baseline = _trade(NOW + timedelta(seconds=603), trade_id=3).model_copy(
        update={"symbol": "BTCUSDT", "price": Decimal("102.03")}
    )
    next_shock = _trade(NOW + timedelta(seconds=604), trade_id=4).model_copy(
        update={"symbol": "BTCUSDT", "price": Decimal("103.06")}
    )

    assert not detector.observe(first)
    assert detector.observe(first_shock)
    assert not detector.observe(next_baseline)
    assert detector.observe(next_shock)
    assert len(sink.triggers) == 2


def test_market_shock_does_not_route_observation_only_symbols(app_config) -> None:
    sink = RecordingTriggerSink()
    detector = MarketShockDetector(
        pipeline_id=app_config.pipeline.version,
        relative_move_threshold=Decimal("0.02"),
        window_seconds=300,
        trigger_expiry_seconds=900,
        sink=sink,
        analysis_owner_symbol="BTCUSDT",
        trigger_symbols=("BTCUSDT", "ETHUSDT"),
    )
    first = _trade(NOW + timedelta(seconds=1), trade_id=1).model_copy(
        update={"symbol": "PAXGUSDT", "price": Decimal("100")}
    )
    shock = _trade(NOW + timedelta(seconds=2), trade_id=2).model_copy(
        update={"symbol": "PAXGUSDT", "price": Decimal("103")}
    )

    assert not detector.observe(first)
    assert not detector.observe(shock)
    assert sink.triggers == []


def test_market_shock_closed_bar_is_fallback_not_duplicate(app_config) -> None:
    sink = RecordingTriggerSink()
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

    fallback_sink = RecordingTriggerSink()
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
    reversal_sink = RecordingTriggerSink()
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

    gap_sink = RecordingTriggerSink()
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
    sink = RecordingTriggerSink()
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
        detector.observe(_trade(at, trade_id=index).model_copy(update={"price": Decimal(price)}))
        for index, (at, price) in enumerate(prices, start=1)
    ]

    assert results == [False, False, True]
    assert len(sink.triggers) == 1


def test_market_shock_rebases_and_cools_down_after_trigger(app_config) -> None:
    sink = RecordingTriggerSink()
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
        detector.observe(_trade(at, trade_id=index).model_copy(update={"price": Decimal(price)}))
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

    spot = InstrumentId.binance_spot(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    assert (
        store.latest_spot_quote(
            instrument=spot,
            evaluation_at=NOW,
            visible_at=NOW,
        )
        == _quote()
    )
    assert (
        store.latest_spot_quote(
            instrument=spot,
            evaluation_at=NOW - timedelta(seconds=1),
            visible_at=NOW,
        )
        is None
    )
    with pytest.raises(ValueError, match="Spot Instrument"):
        store.latest_spot_quote(
            instrument=_perpetual_instrument(),
            evaluation_at=NOW,
            visible_at=NOW,
        )

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


@pytest.mark.parametrize("backend", ["memory", "sql"])
def test_cross_venue_spot_store_is_immutable_and_point_in_time(backend) -> None:
    if backend == "memory":
        store = InMemoryMarketDataStore()
    else:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        create_market_schema(engine)
        store = SqlMarketDataStore(engine)
    current = (
        _cross_venue_quote(SpotVenue.COINBASE, bid="99.9", ask="100.1"),
        _cross_venue_quote(SpotVenue.KRAKEN, bid="99.8", ask="100.2"),
    )
    future = _cross_venue_quote(
        SpotVenue.COINBASE,
        bid="100.9",
        ask="101.1",
        observed_at=NOW + timedelta(minutes=1),
        marker="124",
    )
    assert all(store.put_cross_venue_spot_quote(item) for item in current)
    assert store.put_cross_venue_spot_quote(future)
    assert not store.put_cross_venue_spot_quote(
        current[0].model_copy(
            update={
                "observed_at": NOW + timedelta(seconds=1),
                "source": "recovered-rest",
            }
        )
    )
    assert store.latest_cross_venue_spot_quotes(
        symbol="BTCUSDT",
        venues=(SpotVenue.COINBASE, SpotVenue.KRAKEN),
        as_of=NOW,
    ) == current


@pytest.mark.parametrize("backend", ["memory", "sql"])
def test_perpetual_store_is_immutable_and_point_in_time(backend) -> None:
    if backend == "memory":
        store = InMemoryMarketDataStore()
    else:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        create_market_schema(engine)
        store = SqlMarketDataStore(engine)
    instrument = _perpetual_instrument()
    state = _perpetual_state()
    quote = _perpetual_quote()
    settlement = _funding_settlement()
    schedule = _trading_schedule()
    rules = _perpetual_product_rules()

    assert store.put_perpetual_state(state)
    assert store.put_perpetual_quote(quote)
    assert store.put_funding_settlement(settlement)
    assert store.put_trading_schedule(schedule)
    assert store.put_perpetual_product_rules(rules)
    assert not store.put_perpetual_product_rules(rules)
    assert not store.put_perpetual_state(
        state.model_copy(
            update={
                "observed_at": NOW + timedelta(seconds=1),
                "source": "recovered-rest",
            }
        )
    )
    assert not store.put_funding_settlement(
        settlement.model_copy(
            update={
                "observed_at": NOW + timedelta(seconds=1),
                "source": "recovered-rest",
            }
        )
    )
    assert not store.put_perpetual_quote(
        quote.model_copy(
            update={
                "observed_at": NOW + timedelta(seconds=1),
                "source": "recovered-rest",
            }
        )
    )
    assert not store.put_trading_schedule(
        schedule.model_copy(
            update={
                "observed_at": NOW + timedelta(seconds=1),
                "source": "recovered-rest",
            }
        )
    )
    with pytest.raises(ValueError, match="事实不一致"):
        store.put_perpetual_state(state.model_copy(update={"mark_price": Decimal("101")}))
    with pytest.raises(ValueError, match="事实不一致"):
        store.put_funding_settlement(
            settlement.model_copy(update={"funding_rate": Decimal("0.0002")})
        )
    with pytest.raises(ValueError, match="事实不一致"):
        store.put_perpetual_quote(quote.model_copy(update={"ask": Decimal("100.2")}))
    with pytest.raises(ValueError, match="事实不一致"):
        store.put_trading_schedule(
            schedule.model_copy(
                update={
                    "sessions": (
                        schedule.sessions[0].model_copy(
                            update={"session_type": TradingSessionType.NO_TRADING}
                        ),
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="事实不一致"):
        store.put_perpetual_product_rules(
            rules.model_copy(update={"funding_interval_hours": 4})
        )

    assert (
        store.latest_perpetual_state(
            instrument=instrument,
            as_of=NOW - timedelta(seconds=1),
        )
        is None
    )
    assert store.latest_perpetual_state(instrument=instrument, as_of=NOW) == state
    assert (
        store.latest_perpetual_product_rules(
            instrument=instrument,
            as_of=NOW - timedelta(seconds=1),
        )
        is None
    )
    assert (
        store.latest_perpetual_product_rules(instrument=instrument, as_of=NOW)
        == rules
    )
    assert (
        store.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=NOW - timedelta(seconds=1),
            visible_at=NOW - timedelta(seconds=1),
        )
        is None
    )
    assert (
        store.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=NOW,
            visible_at=NOW,
        )
        == quote
    )
    assert (
        store.funding_settlements(
            instrument=instrument,
            start=NOW - timedelta(hours=8),
            end=NOW - timedelta(seconds=1),
            visible_at=NOW - timedelta(seconds=1),
        )
        == ()
    )
    assert store.funding_settlements(
        instrument=instrument,
        start=NOW - timedelta(hours=8),
        end=NOW,
        visible_at=NOW,
    ) == (settlement,)
    assert (
        store.latest_trading_schedule(as_of=NOW - timedelta(seconds=1)) is None
    )
    assert store.latest_trading_schedule(as_of=NOW) == schedule


def test_perpetual_service_only_refreshes_history_when_settlement_is_due(
    app_config,
) -> None:
    class FakeClient:
        history_calls = 0

        async def fetch_market_state(self, instrument):
            assert instrument == _perpetual_instrument()
            return _perpetual_state()

        async def fetch_quote(self, instrument):
            assert instrument == _perpetual_instrument()
            return _perpetual_quote()

        async def fetch_product_rules(self, instruments):
            assert instruments == (_perpetual_instrument(),)
            return (_perpetual_product_rules(),)

        async def fetch_funding_settlements(self, instrument, *, start, end):
            assert instrument == _perpetual_instrument()
            assert start == NOW - timedelta(hours=720)
            assert end == NOW
            self.history_calls += 1
            return (_funding_settlement(),)

    async def scenario():
        client = FakeClient()
        store = InMemoryMarketDataStore()
        refreshes = []
        policy = app_config.market_data.model_copy(
            update={"perpetual_instruments": (_perpetual_instrument(),)}
        )
        service = BinancePerpetualMarketService(
            policy=policy,
            client=client,  # type: ignore[arg-type]
            store=store,
            refresh_observer=refreshes.append,
            clock=lambda: NOW,
        )
        await service.refresh()
        await service.refresh()
        await service.refresh_quotes()
        return service, client, store, refreshes

    service, client, store, refreshes = asyncio.run(scenario())
    assert client.history_calls == 1
    assert service.health.refresh_count == 2
    assert service.health.quote_refresh_count == 1
    assert service.health.state_count == 1
    assert service.health.quote_count == 1
    assert service.health.settlement_count == 1
    assert refreshes[0].succeeded
    assert refreshes[0].observation_count == 3
    assert refreshes[0].changed_count == 3
    assert refreshes[1].observation_count == 2
    assert refreshes[1].changed_count == 0
    assert (
        store.latest_perpetual_state(
            instrument=_perpetual_instrument(),
            as_of=NOW,
        )
        == _perpetual_state()
    )
    assert (
        store.latest_perpetual_quote(
            instrument=_perpetual_instrument(),
            evaluation_at=NOW,
            visible_at=NOW,
        )
        == _perpetual_quote()
    )


def test_product_rule_success_refreshes_freshness_and_failure_does_not_fake_it(
    app_config,
) -> None:
    clock = [NOW]

    class FakeClient:
        rule_calls = 0

        async def fetch_market_state(self, instrument):
            assert instrument == _perpetual_instrument()
            return _perpetual_state()

        async def fetch_product_rules(self, instruments):
            assert instruments == (_perpetual_instrument(),)
            self.rule_calls += 1
            if self.rule_calls == 3:
                raise TimeoutError("product rules unavailable")
            return (
                _perpetual_product_rules(
                    observed_at=clock[0],
                    upstream_updated_at=NOW - timedelta(seconds=1),
                ),
            )

        async def fetch_funding_settlements(self, instrument, *, start, end):
            assert instrument == _perpetual_instrument()
            return (_funding_settlement(),)

    async def scenario():
        store = InMemoryMarketDataStore()
        refreshes = []
        policy = app_config.market_data.model_copy(
            update={"perpetual_instruments": (_perpetual_instrument(),)}
        )
        service = BinancePerpetualMarketService(
            policy=policy,
            client=FakeClient(),  # type: ignore[arg-type]
            store=store,
            refresh_observer=refreshes.append,
            clock=lambda: clock[0],
        )
        await service.refresh()
        clock[0] = NOW + timedelta(minutes=5)
        await service.refresh()
        clock[0] = NOW + timedelta(minutes=20)
        await service.refresh()
        return service, store, refreshes

    service, store, refreshes = asyncio.run(scenario())
    latest = store.latest_perpetual_product_rules(
        instrument=_perpetual_instrument(),
        as_of=NOW + timedelta(minutes=20),
    )

    assert latest is not None
    assert latest.observed_at == NOW + timedelta(minutes=5)
    assert service.health.product_rule_count == 2
    assert service.health.product_rule_error_class == "TimeoutError"
    assert refreshes[1].changed_count == 0
    assert refreshes[2].succeeded


def test_tradfi_refresh_persists_the_official_schedule_before_market_state(
    app_config,
) -> None:
    instrument = _tradfi_instrument()

    class FakeClient:
        async def fetch_trading_schedule(self):
            return _trading_schedule()

        async def fetch_market_state(self, requested):
            assert requested == instrument
            return _perpetual_state(instrument=instrument)

        async def fetch_product_rules(self, instruments):
            assert instruments == (instrument,)
            return (_perpetual_product_rules(instrument=instrument),)

        async def fetch_funding_settlements(self, requested, *, start, end):
            assert requested == instrument
            assert start == NOW - timedelta(hours=720)
            assert end == NOW
            return (_funding_settlement(instrument=instrument),)

    async def scenario():
        store = InMemoryMarketDataStore()
        policy = app_config.market_data.model_copy(
            update={"perpetual_instruments": (instrument,)}
        )
        refreshes = []
        service = BinancePerpetualMarketService(
            policy=policy,
            client=FakeClient(),  # type: ignore[arg-type]
            store=store,
            refresh_observer=refreshes.append,
            clock=lambda: NOW,
        )
        await service.refresh()
        return service, store, refreshes

    service, store, refreshes = asyncio.run(scenario())
    assert service.health.schedule_count == 1
    assert store.latest_trading_schedule(as_of=NOW) == _trading_schedule()
    assert refreshes[0].observation_count == 4
    assert refreshes[0].changed_count == 4


def test_product_rule_failure_does_not_blind_market_state_or_funding(
    app_config,
) -> None:
    instrument = _perpetual_instrument()

    class RuleFailingClient:
        async def fetch_product_rules(self, instruments):
            assert instruments == (instrument,)
            raise TimeoutError("product rules unavailable")

        async def fetch_market_state(self, requested):
            assert requested == instrument
            return _perpetual_state()

        async def fetch_funding_settlements(self, requested, *, start, end):
            assert requested == instrument
            assert start == NOW - timedelta(hours=720)
            assert end == NOW
            return (_funding_settlement(),)

    async def scenario():
        store = InMemoryMarketDataStore()
        refreshes = []
        policy = app_config.market_data.model_copy(
            update={"perpetual_instruments": (instrument,)}
        )
        service = BinancePerpetualMarketService(
            policy=policy,
            client=RuleFailingClient(),  # type: ignore[arg-type]
            store=store,
            refresh_observer=refreshes.append,
            clock=lambda: NOW,
        )
        await service.refresh()
        return service, store, refreshes

    service, store, refreshes = asyncio.run(scenario())

    assert refreshes[0].succeeded
    assert refreshes[0].observation_count == 2
    assert service.health.product_rule_error_class == "TimeoutError"
    assert store.latest_perpetual_state(instrument=instrument, as_of=NOW) is not None
    assert store.latest_perpetual_product_rules(instrument=instrument, as_of=NOW) is None


def test_perpetual_service_reports_failed_refresh_without_false_success(
    app_config,
) -> None:
    class FailingClient:
        async def fetch_product_rules(self, instruments):
            return (_perpetual_product_rules(),)

        async def fetch_market_state(self, instrument):
            raise TimeoutError("upstream timeout")

        async def fetch_quote(self, instrument):
            return _perpetual_quote()

    async def scenario():
        refreshes = []
        policy = app_config.market_data.model_copy(
            update={"perpetual_instruments": (_perpetual_instrument(),)}
        )
        service = BinancePerpetualMarketService(
            policy=policy,
            client=FailingClient(),  # type: ignore[arg-type]
            store=InMemoryMarketDataStore(),
            refresh_observer=refreshes.append,
            clock=lambda: NOW,
        )
        with pytest.raises(TimeoutError):
            await service.refresh()
        return service, refreshes

    service, refreshes = asyncio.run(scenario())

    assert service.health.refresh_count == 0
    assert len(refreshes) == 1
    assert not refreshes[0].succeeded
    assert refreshes[0].error_class == "TimeoutError"
    assert refreshes[0].observation_count == 0


def test_perpetual_quote_refresh_is_independent_from_slow_state_refresh(
    app_config,
) -> None:
    class StateFailingClient:
        async def fetch_product_rules(self, instruments):
            return (_perpetual_product_rules(),)

        async def fetch_market_state(self, instrument):
            raise TimeoutError("slow state unavailable")

        async def fetch_quote(self, instrument):
            assert instrument == _perpetual_instrument()
            return _perpetual_quote()

    async def scenario():
        store = InMemoryMarketDataStore()
        policy = app_config.market_data.model_copy(
            update={"perpetual_instruments": (_perpetual_instrument(),)}
        )
        service = BinancePerpetualMarketService(
            policy=policy,
            client=StateFailingClient(),  # type: ignore[arg-type]
            store=store,
            clock=lambda: NOW,
        )
        with pytest.raises(TimeoutError):
            await service.refresh()
        await service.refresh_quotes()
        return service, store

    service, store = asyncio.run(scenario())

    assert service.health.refresh_count == 0
    assert service.health.quote_refresh_count == 1
    assert service.health.quote_count == 1
    assert (
        store.latest_perpetual_quote(
            instrument=_perpetual_instrument(),
            evaluation_at=NOW,
            visible_at=NOW,
        )
        == _perpetual_quote()
    )


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
    shadow_config = app_config.model_copy(
        update={
            "deployment": app_config.deployment.model_copy(
                update={
                    "stage": DeploymentStage.SHADOW,
                    "shadow_market_data_enabled": True,
                }
            )
        }
    )
    runtime = assemble_shadow_market_stream(
        shadow_config,
        InMemoryMarketDataStore(),
    )
    assert runtime.perpetual is not None


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
