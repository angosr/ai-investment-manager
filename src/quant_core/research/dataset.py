from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field, field_validator, model_validator

from quant_core.domain import FrozenModel, _require_utc
from quant_core.ids import stable_id
from quant_core.market_data import ClosedMarketBar

_INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class InstrumentSpec(FrozenModel):
    symbol: str
    base_asset: str
    quote_asset: str
    price_increment: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    maximum_quantity: Decimal = Field(gt=0)
    minimum_notional: Decimal = Field(gt=0)
    minimum_price: Decimal = Field(gt=0)
    maximum_price: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def bounds_are_ordered(self):
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("品种最小数量不得大于最大数量")
        if self.minimum_price > self.maximum_price:
            raise ValueError("品种最小价格不得大于最大价格")
        return self


class HistoricalDatasetManifest(FrozenModel):
    schema_version: str = "historical-bars-v1"
    dataset_id: str
    symbol: str
    interval: str
    source: str
    collected_at: datetime
    requested_start: datetime
    requested_end: datetime
    first_open_time: datetime
    last_close_time: datetime
    bar_count: int = Field(gt=0)
    bars_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument: InstrumentSpec

    _utc_collected_at = field_validator("collected_at")(_require_utc)
    _utc_requested_start = field_validator("requested_start")(_require_utc)
    _utc_requested_end = field_validator("requested_end")(_require_utc)
    _utc_first_open_time = field_validator("first_open_time")(_require_utc)
    _utc_last_close_time = field_validator("last_close_time")(_require_utc)

    @model_validator(mode="after")
    def identity_and_bounds_match(self):
        if self.requested_start >= self.requested_end:
            raise ValueError("历史数据请求起点必须早于终点")
        if self.first_open_time < self.requested_start:
            raise ValueError("数据集包含请求起点之前的 K 线")
        if self.first_open_time > self.last_close_time:
            raise ValueError("历史数据时间范围非法")
        expected = stable_id(
            "historical_dataset",
            self.schema_version,
            self.source,
            self.symbol,
            self.interval,
            self.requested_start,
            self.requested_end,
            self.bars_hash,
            self.instrument,
        )
        if self.dataset_id != expected:
            raise ValueError("历史数据集 ID 与冻结内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    manifest: HistoricalDatasetManifest
    bars: tuple[ClosedMarketBar, ...]

    def __post_init__(self) -> None:
        _validate_bars(self.bars, self.manifest)


class HistoricalDatasetCatalog:
    """内容寻址的离线数据目录；同一 ID 的数据绝不原地覆盖。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, dataset: HistoricalDataset) -> Path:
        target = self._root / dataset.manifest.dataset_id
        if target.exists():
            existing = self.load(dataset.manifest.dataset_id)
            if existing.manifest != dataset.manifest:
                raise ValueError("同一历史数据集 ID 的 Manifest 不一致")
            return target

        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".dataset-", dir=self._root))
        try:
            _write_json(temporary / "bars.json", [_compact_bar(item) for item in dataset.bars])
            _write_json(
                temporary / "manifest.json",
                dataset.manifest.model_dump(mode="json"),
            )
            temporary.replace(target)
        except BaseException:
            for item in temporary.iterdir() if temporary.exists() else ():
                item.unlink()
            if temporary.exists():
                temporary.rmdir()
            raise
        return target

    def load(self, dataset_id: str) -> HistoricalDataset:
        target = self._root / dataset_id
        manifest = HistoricalDatasetManifest.model_validate(
            json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        )
        rows = json.loads((target / "bars.json").read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("历史 bars.json 根节点必须是数组")
        bars = tuple(_bar_from_compact(row, manifest) for row in rows)
        return HistoricalDataset(manifest=manifest, bars=bars)


async def fetch_binance_history(
    *,
    base_url: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalDataset:
    """从 Binance 官方 REST 按页抓取完整已收盘 K 线并冻结交易规则。"""

    start = _require_utc(start)
    end = _require_utc(end)
    collected_at = _require_utc((clock or (lambda: datetime.now(UTC)))())
    if start >= end:
        raise ValueError("历史数据请求起点必须早于终点")
    interval_ms = _INTERVAL_MILLISECONDS.get(interval)
    if interval_ms is None:
        raise ValueError(f"不支持的历史 K 线周期: {interval}")
    symbol = symbol.upper()
    if not symbol.isalnum():
        raise ValueError("历史行情 symbol 非法")

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    ) as client:
        instrument_response = await client.get("/api/v3/exchangeInfo", params={"symbol": symbol})
        instrument_response.raise_for_status()
        instrument = _parse_instrument(instrument_response.json(), symbol)

        cursor_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        collected_ms = int(collected_at.timestamp() * 1000)
        rows: list[list[Any]] = []
        while cursor_ms < end_ms:
            response = await client.get(
                "/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor_ms,
                    "endTime": end_ms - 1,
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                raise ValueError("Binance klines REST 响应非法")
            if not page:
                break
            for item in page:
                if not isinstance(item, list) or len(item) < 7:
                    raise ValueError("Binance kline REST 条目非法")
                open_ms = int(item[0])
                close_ms = int(item[6])
                if open_ms < cursor_ms or open_ms >= end_ms or close_ms > collected_ms:
                    continue
                rows.append(
                    [
                        open_ms,
                        str(item[1]),
                        str(item[2]),
                        str(item[3]),
                        str(item[4]),
                        str(item[5]),
                        close_ms,
                    ]
                )
            next_cursor = int(page[-1][0]) + interval_ms
            if next_cursor <= cursor_ms:
                raise ValueError("Binance 历史 K 线分页未前进")
            cursor_ms = next_cursor

    if not rows:
        raise ValueError("指定区间没有完整已收盘 K 线")
    bars = tuple(
        _bar_from_row(row, symbol=symbol, interval=interval, source="binance-rest-historical")
        for row in rows
    )
    bars_hash = _bars_hash(bars)
    manifest_payload = {
        "schema_version": "historical-bars-v1",
        "source": "binance-rest-historical",
        "symbol": symbol,
        "interval": interval,
        "requested_start": start,
        "requested_end": end,
        "bars_hash": bars_hash,
        "instrument": instrument,
    }
    manifest = HistoricalDatasetManifest(
        dataset_id=stable_id("historical_dataset", *manifest_payload.values()),
        symbol=symbol,
        interval=interval,
        source="binance-rest-historical",
        collected_at=collected_at,
        requested_start=start,
        requested_end=end,
        first_open_time=bars[0].open_time,
        last_close_time=bars[-1].close_time,
        bar_count=len(bars),
        bars_hash=bars_hash,
        instrument=instrument,
    )
    return HistoricalDataset(manifest=manifest, bars=bars)


def _parse_instrument(raw: Any, symbol: str) -> InstrumentSpec:
    if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
        raise ValueError("Binance exchangeInfo 响应非法")
    records = [item for item in raw["symbols"] if item.get("symbol") == symbol]
    if len(records) != 1:
        raise ValueError(f"Binance exchangeInfo 未唯一返回 {symbol}")
    record = records[0]
    filters = {
        item.get("filterType"): item
        for item in record.get("filters", [])
        if isinstance(item, dict) and isinstance(item.get("filterType"), str)
    }
    try:
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        notional = filters.get("NOTIONAL") or filters["MIN_NOTIONAL"]
        return InstrumentSpec(
            symbol=symbol,
            base_asset=str(record["baseAsset"]),
            quote_asset=str(record["quoteAsset"]),
            price_increment=Decimal(str(price["tickSize"])),
            quantity_increment=Decimal(str(lot["stepSize"])),
            minimum_quantity=Decimal(str(lot["minQty"])),
            maximum_quantity=Decimal(str(lot["maxQty"])),
            minimum_notional=Decimal(str(notional["minNotional"])),
            minimum_price=Decimal(str(price["minPrice"])),
            maximum_price=Decimal(str(price["maxPrice"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Binance exchangeInfo 缺少必要现货交易规则") from exc


def _validate_bars(
    bars: tuple[ClosedMarketBar, ...], manifest: HistoricalDatasetManifest
) -> None:
    if len(bars) != manifest.bar_count:
        raise ValueError("历史 K 线数量与 Manifest 不一致")
    if not bars:
        raise ValueError("历史数据集不能为空")
    previous: ClosedMarketBar | None = None
    interval_ms = _INTERVAL_MILLISECONDS.get(manifest.interval)
    if interval_ms is None:
        raise ValueError(f"不支持的历史 K 线周期: {manifest.interval}")
    for bar in bars:
        if bar.symbol != manifest.symbol or bar.interval != manifest.interval:
            raise ValueError("历史 K 线作用域与 Manifest 不一致")
        if bar.observed_at != bar.close_time:
            raise ValueError("历史 K 线只能在收盘时刻之后可见")
        if previous is not None and bar.open_time <= previous.open_time:
            raise ValueError("历史 K 线必须按 open_time 严格递增且不重复")
        if previous is not None and int(
            (bar.open_time - previous.open_time).total_seconds() * 1000
        ) != interval_ms:
            raise ValueError("历史 K 线存在缺口；必须补齐或登记为新数据集后再评价")
        previous = bar
    if bars[0].open_time != manifest.first_open_time:
        raise ValueError("历史首根 K 线与 Manifest 不一致")
    if bars[-1].close_time != manifest.last_close_time:
        raise ValueError("历史末根 K 线与 Manifest 不一致")
    if _bars_hash(bars) != manifest.bars_hash:
        raise ValueError("历史 K 线内容哈希与 Manifest 不一致")


def _bars_hash(bars: Iterable[ClosedMarketBar]) -> str:
    digest = hashlib.sha256()
    for bar in bars:
        digest.update(
            json.dumps(_compact_bar(bar), ensure_ascii=False, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _compact_bar(bar: ClosedMarketBar) -> list[Any]:
    return [
        int(bar.open_time.timestamp() * 1000),
        str(bar.open),
        str(bar.high),
        str(bar.low),
        str(bar.close),
        str(bar.volume),
        int(bar.close_time.timestamp() * 1000),
    ]


def _bar_from_compact(row: Any, manifest: HistoricalDatasetManifest) -> ClosedMarketBar:
    if not isinstance(row, list):
        raise ValueError("历史 K 线条目必须是数组")
    return _bar_from_row(
        row,
        symbol=manifest.symbol,
        interval=manifest.interval,
        source=manifest.source,
    )


def _bar_from_row(row: list[Any], *, symbol: str, interval: str, source: str) -> ClosedMarketBar:
    if len(row) != 7:
        raise ValueError("历史 K 线条目必须包含 7 个字段")
    open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
    close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=UTC)
    return ClosedMarketBar(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        observed_at=close_time,
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
        source=source,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
