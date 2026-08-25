from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    TradFiMarket,
)
from investment_manager.market.tables import market_quotes, perpetual_quotes
from investment_manager.platform.database import build_engine
from investment_manager.research.quote_dataset import (
    HistoricalExecutableQuoteCatalog,
    freeze_executable_quotes,
)
from investment_manager.schema import compose_metadata

START = datetime(2026, 8, 25, 9, tzinfo=UTC)


def test_spot_quotes_are_deterministically_sampled_and_content_addressed(
    tmp_path: Path,
) -> None:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    compose_metadata().create_all(engine)
    instrument = InstrumentId.binance_spot(
        symbol="PAXGUSDT",
        base_asset="PAXG",
        quote_asset="USDT",
    )
    times = tuple(START + timedelta(minutes=value) for value in (0, 2, 6, 11))
    with engine.begin() as connection:
        connection.execute(
            insert(market_quotes),
            [
                {
                    "quote_id": f"quote-{index}",
                    "symbol": instrument.symbol,
                    "observed_at": observed_at,
                    "payload": _spot_payload(index, observed_at),
                }
                for index, observed_at in enumerate(times)
            ],
        )

    dataset = freeze_executable_quotes(
        engine,
        instrument=instrument,
        start=START,
        end=START + timedelta(minutes=20),
        sampling_interval_seconds=300,
        captured_at=START + timedelta(hours=1),
    )

    assert dataset.manifest.source_row_count == 4
    assert dataset.manifest.quote_count == 3
    assert tuple(item.source_quote_id for item in dataset.quotes) == (
        "quote-0",
        "quote-2",
        "quote-3",
    )
    catalog = HistoricalExecutableQuoteCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset

    raw = json.loads((target / "quotes.json").read_text(encoding="utf-8"))
    raw[0][3] = "1"
    (target / "quotes.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希"):
        catalog.load(dataset.manifest.dataset_id)
    engine.dispose()


def test_tradfi_quotes_preserve_exchange_time_and_instrument_scope() -> None:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    compose_metadata().create_all(engine)
    instrument = InstrumentId(
        product=InstrumentProduct.TRADFI_PERPETUAL,
        symbol="SPYUSDT",
        base_asset="SPY",
        quote_asset="USDT",
        settlement_asset="USDT",
        tradfi_market=TradFiMarket.EQUITY,
    )
    times = tuple(START + timedelta(minutes=value) for value in (0, 6))
    with engine.begin() as connection:
        connection.execute(
            insert(perpetual_quotes),
            [
                {
                    "quote_id": f"perpetual-{index}",
                    "instrument_id": instrument.key,
                    "exchange_time": observed_at - timedelta(milliseconds=10),
                    "observed_at": observed_at,
                    "payload": _perpetual_payload(index, observed_at, instrument),
                }
                for index, observed_at in enumerate(times)
            ],
        )

    dataset = freeze_executable_quotes(
        engine,
        instrument=instrument,
        start=START,
        end=START + timedelta(minutes=20),
        sampling_interval_seconds=300,
        captured_at=START + timedelta(hours=1),
    )

    assert dataset.manifest.instrument == instrument
    assert all(item.exchange_time is not None for item in dataset.quotes)
    assert dataset.quotes[0].exchange_time == START - timedelta(milliseconds=10)
    engine.dispose()


def _spot_payload(index: int, observed_at: datetime) -> dict[str, object]:
    return {
        "quote_id": f"quote-{index}",
        "symbol": "PAXGUSDT",
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "bid": "4624.22",
        "bid_quantity": "1.7",
        "ask": "4624.23",
        "ask_quantity": "1.8",
        "source": "binance-rest",
        "update_id": None,
    }


def _perpetual_payload(
    index: int,
    observed_at: datetime,
    instrument: InstrumentId,
) -> dict[str, object]:
    exchange_time = observed_at - timedelta(milliseconds=10)
    return {
        "quote_id": f"perpetual-{index}",
        "instrument": instrument.model_dump(mode="json"),
        "exchange_time": exchange_time.isoformat().replace("+00:00", "Z"),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "bid": "767.60",
        "bid_quantity": "8.03",
        "ask": "767.61",
        "ask_quantity": "50.10",
        "source": "binance-usdm-book-ticker-rest",
        "update_id": 1,
    }
