from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import zipfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field, field_validator, model_validator

from investment_manager.domain import IntelligenceEvent
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import ClosedMarketBar

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

_BINANCE_FUNDING_ARCHIVE_SOURCE = "binance-public-data-usdm-funding-rate"
_BINANCE_FUNDING_AVAILABILITY_LAG = timedelta(minutes=1)


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

    _utc_collected_at = field_validator("collected_at")(require_utc)
    _utc_requested_start = field_validator("requested_start")(require_utc)
    _utc_requested_end = field_validator("requested_end")(require_utc)
    _utc_first_open_time = field_validator("first_open_time")(require_utc)
    _utc_last_close_time = field_validator("last_close_time")(require_utc)

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


@dataclass(frozen=True, slots=True)
class HistoricalBarWindow:
    """A verified slice of one immutable dataset for rejection-only research."""

    manifest: HistoricalDatasetManifest
    bars: tuple[ClosedMarketBar, ...]

    def __post_init__(self) -> None:
        if not self.bars:
            raise ValueError("历史 K 线窗口不能为空")
        interval_ms = _INTERVAL_MILLISECONDS.get(self.manifest.interval)
        if interval_ms is None:
            raise ValueError(f"不支持的历史 K 线周期: {self.manifest.interval}")
        previous: ClosedMarketBar | None = None
        for bar in self.bars:
            if bar.symbol != self.manifest.symbol or bar.interval != self.manifest.interval:
                raise ValueError("历史 K 线窗口作用域与 Manifest 不一致")
            if bar.observed_at != bar.close_time:
                raise ValueError("历史 K 线只能在收盘时刻之后可见")
            if previous is not None and int(
                (bar.open_time - previous.open_time).total_seconds() * 1000
            ) != interval_ms:
                raise ValueError("历史 K 线窗口存在缺口")
            previous = bar


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

    def load_window(
        self,
        dataset_id: str,
        *,
        start: datetime,
        end: datetime,
        warmup_bars: int,
    ) -> HistoricalBarWindow:
        """Verify the complete artifact while materializing only a bounded window."""

        start = require_utc(start)
        end = require_utc(end)
        if start >= end:
            raise ValueError("历史 K 线窗口起点必须早于终点")
        if warmup_bars < 0:
            raise ValueError("历史 K 线预热数量不能为负")
        target = self._root / dataset_id
        manifest = HistoricalDatasetManifest.model_validate(
            json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        )
        if start < manifest.first_open_time or end > manifest.requested_end:
            raise ValueError("历史 K 线窗口超出数据集范围")

        digest = hashlib.sha256()
        preceding: deque[list[Any]] = deque(maxlen=warmup_bars)
        selected_rows: list[list[Any]] = []
        row_count = 0
        first_open_time: datetime | None = None
        last_close_time: datetime | None = None
        selected_started = False
        for raw in _iter_json_array(target / "bars.json"):
            if not isinstance(raw, list) or len(raw) != 7:
                raise ValueError("历史 K 线条目必须包含 7 个字段")
            digest.update(
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()
            )
            digest.update(b"\n")
            row_count += 1
            open_time = datetime.fromtimestamp(int(raw[0]) / 1000, tz=UTC)
            close_time = datetime.fromtimestamp(int(raw[6]) / 1000, tz=UTC)
            first_open_time = first_open_time or open_time
            last_close_time = close_time
            if close_time < start:
                preceding.append(raw)
                continue
            if open_time >= end:
                continue
            if not selected_started:
                selected_rows.extend(preceding)
                selected_started = True
            selected_rows.append(raw)

        if (
            row_count != manifest.bar_count
            or first_open_time != manifest.first_open_time
            or last_close_time != manifest.last_close_time
            or digest.hexdigest() != manifest.bars_hash
        ):
            raise ValueError("历史 K 线制品与 Manifest 数量、边界或哈希不一致")
        return HistoricalBarWindow(
            manifest=manifest,
            bars=tuple(_bar_from_compact(row, manifest) for row in selected_rows),
        )


class HistoricalEventDatasetManifest(FrozenModel):
    """Point-in-time event facts, addressed independently from market bars."""

    schema_version: str = "historical-events-v1"
    dataset_id: str
    source: str
    collected_at: datetime
    requested_start: datetime
    requested_end: datetime
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    event_count: int = Field(ge=0)
    events_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_collected_at = field_validator("collected_at")(require_utc)
    _utc_requested_start = field_validator("requested_start")(require_utc)
    _utc_requested_end = field_validator("requested_end")(require_utc)

    @field_validator("first_observed_at", "last_observed_at")
    @classmethod
    def optional_times_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return require_utc(value) if value is not None else None

    @model_validator(mode="after")
    def identity_and_bounds_match(self):
        if self.requested_start >= self.requested_end:
            raise ValueError("历史事件请求起点必须早于终点")
        if self.requested_end > self.collected_at:
            raise ValueError("历史事件窗口终点不能晚于制品冻结时间")
        bounds = (self.first_observed_at, self.last_observed_at)
        if self.event_count == 0 and bounds != (None, None):
            raise ValueError("空事件数据集不能声明观测边界")
        if self.event_count > 0 and (
            self.first_observed_at is None or self.last_observed_at is None
        ):
            raise ValueError("非空事件数据集必须声明观测边界")
        if self.first_observed_at is not None and (
            self.first_observed_at < self.requested_start
            or self.last_observed_at is None
            or self.last_observed_at < self.first_observed_at
            or self.last_observed_at >= self.requested_end
        ):
            raise ValueError("历史事件观测边界与请求窗口不一致")
        expected = stable_id(
            "historical_event_dataset",
            self.schema_version,
            self.source,
            self.requested_start,
            self.requested_end,
            self.events_hash,
        )
        if self.dataset_id != expected:
            raise ValueError("历史事件数据集 ID 与冻结内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class HistoricalEventDataset:
    manifest: HistoricalEventDatasetManifest
    events: tuple[IntelligenceEvent, ...]

    def __post_init__(self) -> None:
        _validate_events(self.events, self.manifest)


class HistoricalEventDatasetCatalog:
    """Immutable event catalog; an empty requested window remains a valid fact."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, dataset: HistoricalEventDataset) -> Path:
        target = self._root / dataset.manifest.dataset_id
        if target.exists():
            existing = self.load(dataset.manifest.dataset_id)
            same_manifest_identity = existing.manifest.model_copy(
                update={"collected_at": dataset.manifest.collected_at}
            ) == dataset.manifest
            if not same_manifest_identity or existing.events != dataset.events:
                raise ValueError("同一历史事件数据集 ID 的内容不一致")
            return target

        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".event-dataset-", dir=self._root))
        try:
            _write_json(
                temporary / "events.json",
                [item.model_dump(mode="json") for item in dataset.events],
            )
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

    def load(self, dataset_id: str) -> HistoricalEventDataset:
        target = self._root / dataset_id
        manifest = HistoricalEventDatasetManifest.model_validate(
            json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        )
        rows = json.loads((target / "events.json").read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("历史 events.json 根节点必须是数组")
        return HistoricalEventDataset(
            manifest=manifest,
            events=tuple(IntelligenceEvent.model_validate(item) for item in rows),
        )


class FundingRateObservation(FrozenModel):
    symbol: str
    funding_time: datetime
    available_at: datetime
    funding_interval_hours: int = Field(gt=0, le=24)
    funding_rate: Decimal

    _utc_funding_time = field_validator("funding_time")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def availability_follows_settlement(self):
        if self.available_at <= self.funding_time:
            raise ValueError("资金费率只能在结算后可见")
        return self


class FundingSourceArtifact(FrozenModel):
    archive_key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HistoricalFundingDatasetManifest(FrozenModel):
    schema_version: Literal["historical-funding-rates-v1"] = (
        "historical-funding-rates-v1"
    )
    dataset_id: str
    symbol: str
    venue: Literal["BINANCE_USDM"] = "BINANCE_USDM"
    source: Literal["binance-public-data-usdm-funding-rate"] = (
        _BINANCE_FUNDING_ARCHIVE_SOURCE
    )
    availability_lag_seconds: Literal[60] = 60
    collected_at: datetime
    requested_start: datetime
    requested_end: datetime
    first_available_at: datetime
    last_available_at: datetime
    observation_count: int = Field(gt=0)
    observations_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifacts: tuple[FundingSourceArtifact, ...] = Field(min_length=1)

    _utc_collected_at = field_validator("collected_at")(require_utc)
    _utc_requested_start = field_validator("requested_start")(require_utc)
    _utc_requested_end = field_validator("requested_end")(require_utc)
    _utc_first_available = field_validator("first_available_at")(require_utc)
    _utc_last_available = field_validator("last_available_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_bounds_match(self):
        if not self.requested_start < self.requested_end <= self.collected_at:
            raise ValueError("历史资金费率请求窗口或冻结时间非法")
        if not (
            self.requested_start < self.first_available_at
            <= self.last_available_at
            < self.requested_end + _BINANCE_FUNDING_AVAILABILITY_LAG
        ):
            raise ValueError("历史资金费率可见边界与请求窗口不一致")
        keys = [item.archive_key for item in self.source_artifacts]
        if keys != sorted(set(keys)):
            raise ValueError("资金费率来源文件必须唯一且有序")
        expected_keys = [
            (
                "data/futures/um/monthly/fundingRate/"
                f"{self.symbol}/{self.symbol}-fundingRate-{year:04d}-{month:02d}.zip"
            )
            for year, month in _months_covering(
                self.requested_start,
                self.requested_end,
            )
        ]
        if keys != expected_keys:
            raise ValueError("资金费率来源月档没有完整覆盖请求窗口")
        expected = stable_id(
            "historical_funding_dataset",
            self.schema_version,
            self.source,
            self.symbol,
            self.venue,
            self.availability_lag_seconds,
            self.requested_start,
            self.requested_end,
            self.observations_hash,
            self.source_artifacts,
        )
        if self.dataset_id != expected:
            raise ValueError("历史资金费率数据集 ID 与冻结内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class HistoricalFundingDataset:
    manifest: HistoricalFundingDatasetManifest
    observations: tuple[FundingRateObservation, ...]

    def __post_init__(self) -> None:
        _validate_funding_observations(self.observations, self.manifest)


class HistoricalFundingDatasetCatalog:
    """Immutable normalized funding rates plus exact official archive checksums."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, dataset: HistoricalFundingDataset) -> Path:
        target = self._root / dataset.manifest.dataset_id
        if target.exists():
            existing = self.load(dataset.manifest.dataset_id)
            same_manifest_identity = existing.manifest.model_copy(
                update={"collected_at": dataset.manifest.collected_at}
            ) == dataset.manifest
            if (
                not same_manifest_identity
                or existing.observations != dataset.observations
            ):
                raise ValueError("同一资金费率数据集 ID 的内容不一致")
            return target

        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".funding-dataset-", dir=self._root))
        try:
            _write_json(
                temporary / "observations.json",
                [_compact_funding(item) for item in dataset.observations],
            )
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

    def load(self, dataset_id: str) -> HistoricalFundingDataset:
        target = self._root / dataset_id
        manifest = HistoricalFundingDatasetManifest.model_validate(
            json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        )
        rows = json.loads((target / "observations.json").read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("历史 funding observations 根节点必须是数组")
        return HistoricalFundingDataset(
            manifest=manifest,
            observations=tuple(_funding_from_compact(row, manifest) for row in rows),
        )


def freeze_historical_events(
    *,
    events: Iterable[IntelligenceEvent],
    source: str,
    requested_start: datetime,
    requested_end: datetime,
    collected_at: datetime,
) -> HistoricalEventDataset:
    """Freeze facts with real arrival times; never infer historical observed_at."""

    start = require_utc(requested_start)
    end = require_utc(requested_end)
    collected = require_utc(collected_at)
    if start >= end:
        raise ValueError("历史事件请求起点必须早于终点")
    if end > collected:
        raise ValueError("历史事件窗口终点不能晚于制品冻结时间")
    ordered = tuple(sorted(events, key=lambda item: (item.observed_at, item.evidence_id)))
    digest = _events_hash(ordered)
    payload = {
        "schema_version": "historical-events-v1",
        "source": source,
        "requested_start": start,
        "requested_end": end,
        "events_hash": digest,
    }
    manifest = HistoricalEventDatasetManifest(
        dataset_id=stable_id("historical_event_dataset", *payload.values()),
        collected_at=collected,
        first_observed_at=ordered[0].observed_at if ordered else None,
        last_observed_at=ordered[-1].observed_at if ordered else None,
        event_count=len(ordered),
        **payload,
    )
    return HistoricalEventDataset(manifest=manifest, events=ordered)


async def fetch_binance_funding_history(
    *,
    base_url: str,
    symbol: str,
    start: datetime,
    end: datetime,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalFundingDataset:
    """Freeze Binance USD-M settled funding rates from checksum-protected archives."""

    if base_url.rstrip("/") != "https://data.binance.vision":
        raise ValueError("资金费率历史只接受 Binance 官方公开数据站")
    start = require_utc(start)
    end = require_utc(end)
    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
    if not start < end <= collected_at:
        raise ValueError("历史资金费率请求窗口或冻结时间非法")
    symbol = symbol.upper()
    if not symbol.isalnum():
        raise ValueError("历史资金费率 symbol 非法")

    rows: list[FundingRateObservation] = []
    artifacts: list[FundingSourceArtifact] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    ) as client:
        for year, month in _months_covering(start, end):
            filename = f"{symbol}-fundingRate-{year:04d}-{month:02d}.zip"
            archive_key = (
                f"data/futures/um/monthly/fundingRate/{symbol}/{filename}"
            )
            archive_response = await client.get(f"/{archive_key}")
            archive_response.raise_for_status()
            checksum_response = await client.get(f"/{archive_key}.CHECKSUM")
            checksum_response.raise_for_status()
            expected_hash = _parse_archive_checksum(
                checksum_response.text,
                filename=filename,
            )
            archive = archive_response.content
            observed_hash = hashlib.sha256(archive).hexdigest()
            if observed_hash != expected_hash:
                raise ValueError(f"Binance 资金费率归档校验失败: {filename}")
            artifacts.append(
                FundingSourceArtifact(
                    archive_key=archive_key,
                    sha256=observed_hash,
                )
            )
            rows.extend(
                _funding_rows_from_archive(
                    archive,
                    filename=filename,
                    symbol=symbol,
                    start=start,
                    end=end,
                )
            )

    observations = tuple(
        sorted(rows, key=lambda item: (item.available_at, item.funding_time))
    )
    if not observations:
        raise ValueError("指定区间没有 Binance 资金费率结算事实")
    digest = _funding_observations_hash(observations)
    source_artifacts = tuple(sorted(artifacts, key=lambda item: item.archive_key))
    payload = {
        "schema_version": "historical-funding-rates-v1",
        "source": _BINANCE_FUNDING_ARCHIVE_SOURCE,
        "symbol": symbol,
        "venue": "BINANCE_USDM",
        "availability_lag_seconds": 60,
        "requested_start": start,
        "requested_end": end,
        "observations_hash": digest,
        "source_artifacts": source_artifacts,
    }
    manifest = HistoricalFundingDatasetManifest(
        dataset_id=stable_id(
            "historical_funding_dataset",
            *payload.values(),
        ),
        collected_at=collected_at,
        first_available_at=observations[0].available_at,
        last_available_at=observations[-1].available_at,
        observation_count=len(observations),
        **payload,
    )
    return HistoricalFundingDataset(
        manifest=manifest,
        observations=observations,
    )


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

    start = require_utc(start)
    end = require_utc(end)
    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
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


def _events_hash(events: Iterable[IntelligenceEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _funding_observations_hash(
    observations: Iterable[FundingRateObservation],
) -> str:
    digest = hashlib.sha256()
    for observation in observations:
        digest.update(
            json.dumps(
                _compact_funding(observation),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_events(
    events: tuple[IntelligenceEvent, ...],
    manifest: HistoricalEventDatasetManifest,
) -> None:
    if len(events) != manifest.event_count:
        raise ValueError("历史事件数量与 Manifest 不一致")
    order = tuple((item.observed_at, item.evidence_id) for item in events)
    if order != tuple(sorted(order)) or len({item.evidence_id for item in events}) != len(
        events
    ):
        raise ValueError("历史事件必须按 observed_at 排序且 evidence_id 唯一")
    if any(item.event_time > item.observed_at for item in events):
        raise ValueError("历史事件不能在发生前被观测")
    if any(
        item.observed_at < manifest.requested_start
        or item.observed_at >= manifest.requested_end
        for item in events
    ):
        raise ValueError("历史事件包含请求窗口外的观测事实")
    first = events[0].observed_at if events else None
    last = events[-1].observed_at if events else None
    if (first, last) != (manifest.first_observed_at, manifest.last_observed_at):
        raise ValueError("历史事件观测边界与 Manifest 不一致")
    if _events_hash(events) != manifest.events_hash:
        raise ValueError("历史事件内容哈希与 Manifest 不一致")


def _validate_funding_observations(
    observations: tuple[FundingRateObservation, ...],
    manifest: HistoricalFundingDatasetManifest,
) -> None:
    if len(observations) != manifest.observation_count or not observations:
        raise ValueError("历史资金费率数量与 Manifest 不一致")
    order = tuple(
        (item.available_at, item.funding_time) for item in observations
    )
    if order != tuple(sorted(order)):
        raise ValueError("历史资金费率必须按可见时间严格排序")
    if len({item.funding_time for item in observations}) != len(observations):
        raise ValueError("历史资金费率结算时间不得重复")
    if any(item.symbol != manifest.symbol for item in observations):
        raise ValueError("历史资金费率品种与 Manifest 不一致")
    if any(
        item.funding_time < manifest.requested_start
        or item.funding_time >= manifest.requested_end
        or item.available_at
        != item.funding_time
        + timedelta(seconds=manifest.availability_lag_seconds)
        for item in observations
    ):
        raise ValueError("历史资金费率包含窗口外事实或可见延迟不一致")
    for previous, current in pairwise(observations):
        if current.funding_time - previous.funding_time > timedelta(
            hours=previous.funding_interval_hours,
            minutes=1,
        ):
            raise ValueError("历史资金费率结算序列存在缺口")
    if (
        observations[0].funding_time - manifest.requested_start
        > timedelta(hours=observations[0].funding_interval_hours, minutes=1)
        or manifest.requested_end - observations[-1].funding_time
        > timedelta(hours=observations[-1].funding_interval_hours, minutes=1)
    ):
        raise ValueError("历史资金费率首尾没有覆盖完整请求窗口")
    if (
        observations[0].available_at != manifest.first_available_at
        or observations[-1].available_at != manifest.last_available_at
    ):
        raise ValueError("历史资金费率可见边界与 Manifest 不一致")
    if _funding_observations_hash(observations) != manifest.observations_hash:
        raise ValueError("历史资金费率内容哈希与 Manifest 不一致")


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


def _compact_funding(observation: FundingRateObservation) -> list[Any]:
    return [
        int(observation.funding_time.timestamp() * 1000),
        int(observation.available_at.timestamp() * 1000),
        observation.funding_interval_hours,
        str(observation.funding_rate),
    ]


def _funding_from_compact(
    row: Any,
    manifest: HistoricalFundingDatasetManifest,
) -> FundingRateObservation:
    if not isinstance(row, list) or len(row) != 4:
        raise ValueError("历史资金费率条目必须包含 4 个字段")
    return FundingRateObservation(
        symbol=manifest.symbol,
        funding_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
        available_at=datetime.fromtimestamp(int(row[1]) / 1000, tz=UTC),
        funding_interval_hours=int(row[2]),
        funding_rate=Decimal(str(row[3])),
    )


def _months_covering(start: datetime, end: datetime) -> tuple[tuple[int, int], ...]:
    year, month = start.year, start.month
    result: list[tuple[int, int]] = []
    while datetime(year, month, 1, tzinfo=UTC) < end:
        result.append((year, month))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return tuple(result)


def _parse_archive_checksum(raw: str, *, filename: str) -> str:
    parts = raw.strip().split()
    if (
        len(parts) != 2
        or parts[1].lstrip("*") != filename
        or len(parts[0]) != 64
        or any(character not in "0123456789abcdef" for character in parts[0])
    ):
        raise ValueError(f"Binance 资金费率 CHECKSUM 非法: {filename}")
    return parts[0]


def _funding_rows_from_archive(
    archive: bytes,
    *,
    filename: str,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[FundingRateObservation]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            expected_csv = filename.removesuffix(".zip") + ".csv"
            if zipped.namelist() != [expected_csv]:
                raise ValueError("归档必须且只能包含对应 CSV")
            text = zipped.read(expected_csv).decode("utf-8")
    except (UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"Binance 资金费率归档非法: {filename}") from exc
    reader = csv.DictReader(io.StringIO(text))
    expected_fields = [
        "calc_time",
        "funding_interval_hours",
        "last_funding_rate",
    ]
    if reader.fieldnames != expected_fields:
        raise ValueError(f"Binance 资金费率 CSV Schema 非法: {filename}")
    rows: list[FundingRateObservation] = []
    try:
        for raw in reader:
            funding_time = datetime.fromtimestamp(
                int(raw["calc_time"]) / 1000,
                tz=UTC,
            )
            if not start <= funding_time < end:
                continue
            rows.append(
                FundingRateObservation(
                    symbol=symbol,
                    funding_time=funding_time,
                    available_at=funding_time + _BINANCE_FUNDING_AVAILABILITY_LAG,
                    funding_interval_hours=int(raw["funding_interval_hours"]),
                    funding_rate=Decimal(raw["last_funding_rate"]),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Binance 资金费率 CSV 条目非法: {filename}") from exc
    return rows


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


def _iter_json_array(path: Path, *, chunk_size: int = 1 << 20) -> Iterator[Any]:
    """Stream the owned compact JSON-array format without trusting partial data."""

    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    started = False
    ended = False
    with path.open(encoding="utf-8") as source:
        while True:
            chunk = source.read(chunk_size)
            eof = chunk == ""
            buffer += chunk
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if not started:
                    if position >= len(buffer):
                        break
                    if buffer[position] != "[":
                        raise ValueError("历史 bars.json 根节点必须是数组")
                    started = True
                    position += 1
                    continue
                while position < len(buffer) and (
                    buffer[position].isspace() or buffer[position] == ","
                ):
                    position += 1
                if position >= len(buffer):
                    break
                if buffer[position] == "]":
                    ended = True
                    position += 1
                    break
                try:
                    value, position = decoder.raw_decode(buffer, position)
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ValueError("历史 bars.json 包含不完整或非法 JSON") from exc
                    break
                yield value
            if position:
                buffer = buffer[position:]
                position = 0
            if ended:
                if buffer.strip() or source.read(1):
                    raise ValueError("历史 bars.json 数组后包含多余内容")
                return
            if eof:
                break
    raise ValueError("历史 bars.json 数组未正确结束")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
