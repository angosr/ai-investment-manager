from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import (
    InstrumentId,
    MarketQuote,
    MarketReferencePrice,
)
from investment_manager.market.repository import SqlMarketDataStore, create_market_schema
from investment_manager.research.reference_product_mapping import (
    ReferenceProductMappingArtifact,
    freeze_reference_product_mapping,
    store_reference_product_mapping,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _reference(
    exchange_time: datetime,
    observed_at: datetime,
    price: str,
) -> MarketReferencePrice:
    return MarketReferencePrice(
        reference_price_id=stable_id(
            "binance_spot_reference_price",
            "SPYBUSDT",
            exchange_time.isoformat(),
        ),
        symbol="SPYBUSDT",
        exchange_time=exchange_time,
        observed_at=observed_at,
        price=price,
        source="binance-spot-reference-price-websocket",
    )


def _quote(observed_at: datetime, bid: str, ask: str, marker: int) -> MarketQuote:
    return MarketQuote(
        quote_id=stable_id("test_spyb_quote", marker),
        symbol="SPYBUSDT",
        observed_at=observed_at,
        bid=bid,
        bid_quantity="10",
        ask=ask,
        ask_quantity="10",
        update_id=marker,
        source="test",
    )


def test_reference_product_mapping_is_point_in_time_and_content_addressed(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_market_schema(engine)
    store = SqlMarketDataStore(engine)
    before_window = _reference(
        NOW - timedelta(milliseconds=500),
        NOW - timedelta(milliseconds=100),
        "100",
    )
    in_window = _reference(
        NOW + timedelta(seconds=1),
        NOW + timedelta(seconds=1, milliseconds=100),
        "101",
    )
    assert store.put_reference_price(before_window)
    assert store.put_reference_price(in_window)
    assert store.put_quote(_quote(NOW + timedelta(milliseconds=100), "99.9", "100.1", 1))
    assert store.put_quote(
        _quote(NOW + timedelta(seconds=1, milliseconds=200), "100.9", "101.1", 2)
    )
    assert store.put_quote(
        _quote(NOW + timedelta(seconds=4, milliseconds=200), "102", "102.2", 3)
    )
    instrument = InstrumentId.binance_spot(
        symbol="SPYBUSDT",
        base_asset="SPYB",
        quote_asset="USDT",
    )

    artifact = freeze_reference_product_mapping(
        engine,
        instrument=instrument,
        start=NOW,
        end=NOW + timedelta(seconds=5),
        sampling_interval_seconds=1,
        maximum_reference_age_ms=3_000,
        reference_contract="https://developers.binance.com/reference-price",
        reference_calculation_type="EXTERNAL",
        reference_external_calculation_id=2,
        captured_at=NOW + timedelta(seconds=6),
    )

    assert artifact.source_quote_count == 3
    assert artifact.source_reference_count == 2
    assert artifact.sampled_quote_count == 3
    assert artifact.matched_quote_count == 2
    assert artifact.unmatched_quote_count == 1
    assert artifact.matched_fraction == Decimal("2") / Decimal("3")
    assert tuple(item.reference_price_id for item in artifact.observations) == (
        before_window.reference_price_id,
        in_window.reference_price_id,
    )
    assert tuple(item.mid_premium_bps for item in artifact.observations) == (
        Decimal("0"),
        Decimal("0"),
    )
    target = store_reference_product_mapping(artifact, root=tmp_path)
    assert store_reference_product_mapping(artifact, root=tmp_path) == target
    assert ReferenceProductMappingArtifact.model_validate_json(
        target.read_text(encoding="utf-8")
    ) == artifact
