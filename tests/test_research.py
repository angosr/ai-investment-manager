from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import typer

from investment_manager.entrypoints.cli.support import parse_research_symbol
from investment_manager.information.models import IntelligenceEvent
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import ClosedMarketBar
from investment_manager.market.perpetual.models import FundingRateType
from investment_manager.research.carry import (
    HistoricalCarryDatasetCatalog,
    fetch_binance_carry_history,
)
from investment_manager.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetCatalog,
    HistoricalDatasetManifest,
    HistoricalEventDatasetCatalog,
    HistoricalFundingDatasetCatalog,
    InstrumentSpec,
    _bars_hash,
    fetch_binance_funding_history,
    fetch_binance_history,
    fetch_binance_usdm_history,
    freeze_historical_events,
)


def test_public_data_research_symbol_is_independent_of_production_allowlist(
    app_config,
) -> None:
    assert "BNBUSDT" not in app_config.market_data.symbols
    assert "BNBUSDT" not in {
        item.instrument.symbol for item in app_config.capital.execution_specs
    }
    assert parse_research_symbol("bnbusdt") == "BNBUSDT"

    with pytest.raises(typer.BadParameter, match="字母和数字"):
        parse_research_symbol("BNB/USDT")


def test_history_command_overrides_production_symbol_and_interval(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    from investment_manager.entrypoints.cli.research_commands import (
        fetch_binance_history_command,
    )
    from investment_manager.research import dataset as dataset_module

    instrument = _instrument().model_copy(
        update={"symbol": "BNBUSDT", "base_asset": "BNB"}
    )
    frozen = _dataset(
        count=2,
        interval="1d",
        bar_delta=timedelta(days=1),
        instrument=instrument,
    )
    captured: dict[str, object] = {}

    async def fake_fetch(**kwargs):
        captured.update(kwargs)
        return frozen

    monkeypatch.setattr(dataset_module, "fetch_binance_history", fake_fetch)
    fetch_binance_history_command(
        config=Path("config/investment-manager.shadow.yaml"),
        symbol="bnbusdt",
        start="2026-01-01T00:00:00Z",
        end="2026-01-03T00:00:00Z",
        interval="1d",
        catalog=tmp_path,
    )

    payload = json.loads(capsys.readouterr().out)
    assert captured["symbol"] == payload["symbol"] == "BNBUSDT"
    assert captured["interval"] == payload["interval"] == "1d"

def _instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.000001"),
        minimum_quantity=Decimal("0.000001"),
        maximum_quantity=Decimal("9000"),
        minimum_notional=Decimal("10"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("1000000"),
    )


def _dataset(
    *,
    count: int = 500,
    price_step: Decimal = Decimal("1.0002"),
    price_steps: tuple[Decimal, ...] | None = None,
    interval: str = "5m",
    bar_delta: timedelta = timedelta(minutes=5),
    initial_price: Decimal = Decimal("10000"),
    instrument: InstrumentSpec | None = None,
    source: str = "test-history",
) -> HistoricalDataset:
    if price_steps is not None and len(price_steps) != count:
        raise ValueError("price_steps 必须与 count 一致")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + bar_delta * count
    bars: list[ClosedMarketBar] = []
    spec = instrument or _instrument()
    price = initial_price
    for index in range(count):
        open_price = price
        close_price = open_price * (
            price_steps[index] if price_steps is not None else price_step
        )
        price = close_price
        open_time = start + bar_delta * index
        close_time = open_time + bar_delta - timedelta(milliseconds=1)
        bars.append(
            ClosedMarketBar(
                symbol=spec.symbol,
                interval=interval,
                open_time=open_time,
                close_time=close_time,
                observed_at=close_time,
                open=open_price,
                high=max(open_price, close_price) * Decimal("1.0001"),
                low=min(open_price, close_price) * Decimal("0.9999"),
                close=close_price,
                volume=Decimal("10"),
                source="test-history",
            )
        )
    bars_hash = _bars_hash(bars)
    dataset_id = stable_id(
        "historical_dataset",
        "historical-bars-v1",
        source,
        spec.symbol,
        interval,
        start,
        end,
        bars_hash,
        spec,
    )
    manifest = HistoricalDatasetManifest(
        dataset_id=dataset_id,
        symbol=spec.symbol,
        interval=interval,
        source=source,
        collected_at=end,
        requested_start=start,
        requested_end=end,
        first_open_time=bars[0].open_time,
        last_close_time=bars[-1].close_time,
        bar_count=len(bars),
        bars_hash=bars_hash,
        instrument=spec,
    )
    return HistoricalDataset(manifest=manifest, bars=tuple(bars))


def test_historical_catalog_round_trip_and_rejects_tampering(
    tmp_path,
) -> None:
    dataset = _dataset(count=10)
    catalog = HistoricalDatasetCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset
    window = catalog.load_window(
        dataset.manifest.dataset_id,
        start=dataset.bars[5].close_time,
        end=dataset.manifest.requested_end,
        warmup_bars=2,
    )
    assert window.bars == dataset.bars[3:]
    rows = json.loads((target / "bars.json").read_text())
    rows[0][4] = "9999"
    (target / "bars.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(dataset.manifest.dataset_id)
    with pytest.raises(ValueError, match="哈希"):
        catalog.load_window(
            dataset.manifest.dataset_id,
            start=dataset.bars[5].close_time,
            end=dataset.manifest.requested_end,
            warmup_bars=2,
        )


def test_historical_dataset_rejects_bar_gap() -> None:
    dataset = _dataset(count=10)
    bars = list(dataset.bars)
    bars.pop(4)
    bars_hash = _bars_hash(bars)
    manifest = HistoricalDatasetManifest(
        dataset_id=stable_id(
            "historical_dataset",
            dataset.manifest.schema_version,
            dataset.manifest.source,
            dataset.manifest.symbol,
            dataset.manifest.interval,
            dataset.manifest.requested_start,
            dataset.manifest.requested_end,
            bars_hash,
            dataset.manifest.instrument,
        ),
        **dataset.manifest.model_dump(
            exclude={"dataset_id", "bar_count", "bars_hash"}
        ),
        bar_count=len(bars),
        bars_hash=bars_hash,
    )
    with pytest.raises(ValueError, match="存在缺口"):
        HistoricalDataset(manifest=manifest, bars=tuple(bars))


def _event(*, observed_at: datetime, evidence_id: str = "event-1") -> IntelligenceEvent:
    return IntelligenceEvent(
        evidence_id=evidence_id,
        normalizer_version="test-normalizer-v1",
        acquisition_route="test-archive-v1",
        event_time=observed_at - timedelta(seconds=30),
        observed_at=observed_at,
        source="test-source",
        title=f"point-in-time {evidence_id}",
        body="historical event body",
        symbols=("BTCUSDT",),
        relevance=Decimal("1"),
        impact=Decimal("0.8"),
        source_reliability=Decimal("0.7"),
        novelty=Decimal("1"),
    )


def test_historical_event_catalog_round_trip_and_rejects_tampering(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    dataset = freeze_historical_events(
        events=(
            _event(observed_at=start + timedelta(hours=2), evidence_id="event-2"),
            _event(observed_at=start + timedelta(hours=1), evidence_id="event-1"),
        ),
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end,
    )
    catalog = HistoricalEventDatasetCatalog(tmp_path)
    target = catalog.store(dataset)

    assert catalog.load(dataset.manifest.dataset_id) == dataset
    assert tuple(item.evidence_id for item in dataset.events) == ("event-1", "event-2")
    repeated = freeze_historical_events(
        events=dataset.events,
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end + timedelta(hours=1),
    )
    assert catalog.store(repeated) == target

    rows = json.loads((target / "events.json").read_text())
    rows[0]["body"] = "tampered"
    (target / "events.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(dataset.manifest.dataset_id)


def test_historical_event_freeze_accepts_empty_window_as_observed_fact() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    dataset = freeze_historical_events(
        events=(),
        source="test-archive",
        requested_start=start,
        requested_end=start + timedelta(hours=1),
        collected_at=start + timedelta(hours=1),
    )

    assert dataset.manifest.event_count == 0
    assert dataset.manifest.first_observed_at is None

    with pytest.raises(ValueError, match="终点不能晚于"):
        freeze_historical_events(
            events=(),
            source="test-archive",
            requested_start=start,
            requested_end=start + timedelta(hours=2),
            collected_at=start + timedelta(hours=1),
        )


def test_fetch_binance_history_paginates_and_freezes_instrument(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    first_ms = int(start.timestamp() * 1000)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "tickSize": "0.01",
                                    "minPrice": "0.01",
                                    "maxPrice": "1000000",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.000001",
                                    "minQty": "0.000001",
                                    "maxQty": "9000",
                                },
                                {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                            ],
                        }
                    ]
                },
            )
        calls += 1
        if calls > 1:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                [
                    first_ms,
                    "100",
                    "102",
                    "99",
                    "101",
                    "5",
                    first_ms + 299_999,
                    "505",
                    12,
                    "3",
                    "303",
                    "0",
                ],
                [
                    first_ms + 300_000,
                    "101",
                    "103",
                    "100",
                    "102",
                    "6",
                    first_ms + 599_999,
                    "612",
                    14,
                    "4",
                    "408",
                    "0",
                ],
            ],
        )

    dataset = asyncio.run(
        fetch_binance_history(
            base_url="https://api.binance.com",
            symbol="BTCUSDT",
            interval="5m",
            start=start,
            end=end,
            timeout_seconds=1,
            clock=lambda: end + timedelta(days=1),
            transport=httpx.MockTransport(handler),
        )
    )
    assert dataset.manifest.bar_count == 2
    assert dataset.manifest.schema_version == "historical-bars-v2"
    assert dataset.manifest.instrument.quantity_increment == Decimal("0.000001")
    assert dataset.bars[0].observed_at == dataset.bars[0].close_time
    assert dataset.bars[0].quote_volume == Decimal("505")
    assert dataset.bars[0].taker_buy_base_volume == Decimal("3")
    assert dataset.bars[0].taker_buy_quote_volume == Decimal("303")
    catalog = HistoricalDatasetCatalog(tmp_path)
    catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset
    assert calls == 1


def test_fetch_binance_usdm_history_has_distinct_source_and_rules() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    first_ms = int(start.timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "tickSize": "0.1",
                                    "minPrice": "0.1",
                                    "maxPrice": "1000000",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.001",
                                    "minQty": "0.001",
                                    "maxQty": "1000",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "100"},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json=[
                [
                    first_ms,
                    "100",
                    "102",
                    "99",
                    "101",
                    "5",
                    first_ms + 299_999,
                    "505",
                    12,
                    "3",
                    "303",
                    "0",
                ],
                [
                    first_ms + 300_000,
                    "101",
                    "103",
                    "100",
                    "102",
                    "6",
                    first_ms + 599_999,
                    "612",
                    14,
                    "4",
                    "408",
                    "0",
                ],
            ],
        )

    dataset = asyncio.run(
        fetch_binance_usdm_history(
            base_url="https://fapi.binance.com",
            symbol="BTCUSDT",
            interval="5m",
            start=start,
            end=end,
            timeout_seconds=1,
            clock=lambda: end + timedelta(days=1),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.source == "binance-usdm-rest-historical"
    assert dataset.manifest.schema_version == "historical-bars-v2"
    assert dataset.manifest.instrument.quantity_increment == Decimal("0.001")
    assert dataset.manifest.instrument.minimum_notional == Decimal("100")
    assert dataset.bars[0].source == dataset.manifest.source
    assert dataset.bars[0].taker_buy_base_volume == Decimal("3")
    with pytest.raises(ValueError, match="官方 REST"):
        asyncio.run(
            fetch_binance_usdm_history(
                base_url="https://example.com",
                symbol="BTCUSDT",
                interval="5m",
                start=start,
                end=end,
                timeout_seconds=1,
            )
        )


def _funding_archive(filename: str, rows: tuple[tuple[str, str, str], ...]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        body = "calc_time,funding_interval_hours,last_funding_rate\n" + "".join(
            f"{timestamp},{interval},{rate}\n"
            for timestamp, interval, rate in rows
        )
        archive.writestr(filename.removesuffix(".zip") + ".csv", body)
    return stream.getvalue()


def test_funding_history_verifies_archive_and_freezes_post_settlement_visibility(
    tmp_path,
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(hours=16)
    first_ms = int(start.timestamp() * 1000)
    filename = "BTCUSDT-fundingRate-2026-07.zip"
    archive = _funding_archive(
        filename,
        (
            (str(first_ms), "8", "0.0001"),
            (str(first_ms + 8 * 60 * 60 * 1000), "8", "-0.00005"),
        ),
    )
    checksum = hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{checksum}  {filename}\n")
        return httpx.Response(200, content=archive)

    dataset = asyncio.run(
        fetch_binance_funding_history(
            base_url="https://data.binance.vision",
            symbol="BTCUSDT",
            start=start,
            end=end,
            timeout_seconds=1,
            clock=lambda: datetime(2026, 8, 1, tzinfo=UTC),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.observation_count == 2
    assert dataset.observations[0].available_at == start + timedelta(minutes=1)
    assert dataset.observations[1].funding_rate == Decimal("-0.00005")
    assert dataset.manifest.source_artifacts[0].sha256 == checksum
    catalog = HistoricalFundingDatasetCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset
    rows = json.loads((target / "observations.json").read_text())
    rows[0][3] = "9"
    (target / "observations.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(dataset.manifest.dataset_id)


def test_verified_funding_history_preserves_special_cashflows_and_interval_changes(
    tmp_path,
) -> None:
    start = datetime(2026, 6, 18, 0, 0, 0, 1000, tzinfo=UTC)
    end = start + timedelta(hours=16)
    times = (
        start,
        start + timedelta(microseconds=8000),
        start + timedelta(hours=1),
        start + timedelta(hours=2),
        start + timedelta(hours=3),
        start + timedelta(hours=4),
        start + timedelta(hours=8),
    )
    intervals = ("1", "1", "1", "1", "1", "1", "8")
    rates = (
        "0.00002292",
        "-0.00239112",
        "0.00003682",
        "0",
        "0",
        "0.00000286",
        "0.00019584",
    )
    filename = "SPYUSDT-fundingRate-2026-06.zip"
    archive = _funding_archive(
        filename,
        tuple(
            (
                str(int(at.timestamp() * 1000)),
                interval,
                rate,
            )
            for at, interval, rate in zip(times, intervals, rates, strict=True)
        ),
    )
    checksum = hashlib.sha256(archive).hexdigest()
    rest_rows = [
        {
            "symbol": "SPYUSDT",
            "fundingTime": int(at.timestamp() * 1000),
            "fundingRate": rate,
            "markPrice": "746.2",
            "rateType": "Special" if index == 1 else "Regular",
        }
        for index, (at, rate) in enumerate(zip(times, rates, strict=True))
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fapi.binance.com":
            return httpx.Response(200, json=rest_rows)
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{checksum}  {filename}\n")
        return httpx.Response(200, content=archive)

    dataset = asyncio.run(
        fetch_binance_funding_history(
            base_url="https://data.binance.vision",
            verification_base_url="https://fapi.binance.com",
            symbol="SPYUSDT",
            start=start,
            end=end,
            timeout_seconds=1,
            clock=lambda: datetime(2026, 7, 1, tzinfo=UTC),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.schema_version == "historical-funding-rates-v2"
    assert dataset.manifest.verification_source == "binance-usdm-funding-rest"
    assert dataset.manifest.verification_hash is not None
    assert dataset.observations[1].rate_type == FundingRateType.SPECIAL
    assert dataset.observations[1].mark_price == Decimal("746.2")
    catalog = HistoricalFundingDatasetCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset
    assert len(json.loads((target / "observations.json").read_text())[0]) == 6


def test_funding_history_rejects_untrusted_source_and_checksum() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(hours=8)
    filename = "BTCUSDT-fundingRate-2026-07.zip"
    archive = _funding_archive(
        filename,
        ((str(int(start.timestamp() * 1000)), "8", "0.0001"),),
    )

    with pytest.raises(ValueError, match="官方公开数据站"):
        asyncio.run(
            fetch_binance_funding_history(
                base_url="https://example.com",
                symbol="BTCUSDT",
                start=start,
                end=end,
                timeout_seconds=1,
                clock=lambda: end,
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{'0' * 64}  {filename}\n")
        return httpx.Response(200, content=archive)

    with pytest.raises(ValueError, match="归档校验失败"):
        asyncio.run(
            fetch_binance_funding_history(
                base_url="https://data.binance.vision",
                symbol="BTCUSDT",
                start=start,
                end=end,
                timeout_seconds=1,
                clock=lambda: datetime(2026, 8, 1, tzinfo=UTC),
                transport=httpx.MockTransport(handler),
            )
        )


def test_funding_history_rejects_unfinished_archive_month_before_network() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 25, tzinfo=UTC)

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("未完成月份不得请求月度归档")

    with pytest.raises(ValueError, match="不覆盖未完成月份"):
        asyncio.run(
            fetch_binance_funding_history(
                base_url="https://data.binance.vision",
                symbol="SPYUSDT",
                start=start,
                end=end,
                timeout_seconds=1,
                clock=lambda: end,
                transport=httpx.MockTransport(unexpected_request),
            )
        )


def test_carry_history_aligns_all_series_and_verifies_funding_marks(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    spot_dataset = _dataset(
        count=2,
        interval="1d",
        bar_delta=timedelta(days=1),
        initial_price=Decimal("100"),
    )
    end = spot_dataset.manifest.requested_end
    first_ms = int(start.timestamp() * 1000)
    filename = "BTCUSDT-fundingRate-2026-01.zip"
    funding_rows = tuple(
        (
            str(first_ms + index * 8 * 60 * 60 * 1000),
            "8",
            "0.0001",
        )
        for index in range(6)
    )
    archive = _funding_archive(filename, funding_rows)
    checksum = hashlib.sha256(archive).hexdigest()

    def funding_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{checksum}  {filename}\n")
        return httpx.Response(200, content=archive)

    funding_dataset = asyncio.run(
        fetch_binance_funding_history(
            base_url="https://data.binance.vision",
            symbol="BTCUSDT",
            start=start,
            end=end,
            timeout_seconds=1,
            clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
            transport=httpx.MockTransport(funding_handler),
        )
    )

    def carry_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "pair": "BTCUSDT",
                            "contractType": "PERPETUAL",
                            "status": "TRADING",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                            "onboardDate": first_ms - 86_400_000,
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.001",
                                    "minQty": "0.001",
                                    "maxQty": "1000",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/fundingRate":
            cursor = int(request.url.params["startTime"])
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": int(timestamp) + index % 2,
                    "fundingRate": rate,
                    "markPrice": str(Decimal("100") + index),
                }
                for index, (timestamp, _, rate) in enumerate(funding_rows)
                if int(timestamp) >= cursor
            ]
            return httpx.Response(200, json=rows)
        if (
            request.url.path == "/fapi/v1/markPriceKlines"
            and request.url.params["interval"] == "8h"
        ):
            rows = []
            for index in range(7):
                open_ms = first_ms - 8 * 60 * 60 * 1000 + index * 8 * 60 * 60 * 1000
                rows.append(
                    [
                        open_ms,
                        "100",
                        "102",
                        "99",
                        str(Decimal("100") + index),
                        "0",
                        open_ms + 8 * 60 * 60 * 1000 - 1,
                    ]
                )
            return httpx.Response(200, json=rows)
        rows = [
            [first_ms, "100", "102", "99", "101", "0", first_ms + 86_399_999],
            [
                first_ms + 86_400_000,
                "101",
                "103",
                "100",
                "102",
                "0",
                first_ms + 2 * 86_400_000 - 1,
            ],
        ]
        if request.url.path == "/fapi/v1/premiumIndexKlines":
            rows = [
                [row[0], "-0.001", "0.002", "-0.003", "0.001", "0", row[6]]
                for row in rows
            ]
        return httpx.Response(200, json=rows)

    dataset = asyncio.run(
        fetch_binance_carry_history(
            base_url="https://fapi.binance.com",
            spot_dataset=spot_dataset,
            funding_dataset=funding_dataset,
            timeout_seconds=1,
            clock=lambda: end + timedelta(days=1),
            transport=httpx.MockTransport(carry_handler),
        )
    )

    assert dataset.manifest.day_count == 2
    assert dataset.manifest.settlement_count == 6
    assert dataset.settlements[1].mark_price == Decimal("101")
    assert dataset.days[0].premium_low == Decimal("-0.003")
    catalog = HistoricalCarryDatasetCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset

    rows = json.loads((target / "settlements.json").read_text())
    rows[0][4] = "999"
    (target / "settlements.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(dataset.manifest.dataset_id)


def test_carry_history_rejects_untrusted_source() -> None:
    spot_dataset = _dataset(count=2, interval="1d", bar_delta=timedelta(days=1))
    with pytest.raises(ValueError, match="官方 REST"):
        asyncio.run(
            fetch_binance_carry_history(
                base_url="https://example.com",
                spot_dataset=spot_dataset,
                funding_dataset=None,  # type: ignore[arg-type]
                timeout_seconds=1,
            )
        )
