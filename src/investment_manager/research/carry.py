from __future__ import annotations

import hashlib
import json
import tempfile
from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field, field_validator, model_validator

from investment_manager.domain import FrozenModel, _require_utc
from investment_manager.kernel.identity import stable_id
from investment_manager.research.dataset import HistoricalDataset, HistoricalFundingDataset

_BINANCE_USDM_REST = "https://fapi.binance.com"
_DAY_MILLISECONDS = 86_400_000
_FUNDING_TIME_TOLERANCE_MILLISECONDS = 5_000


class CarryInstrumentSpec(FrozenModel):
    """USD-M perpetual rules captured with the research data bundle."""

    symbol: str
    pair: str
    contract_type: Literal["PERPETUAL"] = "PERPETUAL"
    status: Literal["TRADING"] = "TRADING"
    base_asset: str
    quote_asset: str
    margin_asset: str
    onboarded_at: datetime
    price_increment: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    maximum_quantity: Decimal = Field(gt=0)
    minimum_notional: Decimal = Field(gt=0)

    _utc_onboarded_at = field_validator("onboarded_at")(_require_utc)

    @model_validator(mode="after")
    def bounds_are_ordered(self):
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("永续合约最小数量不得大于最大数量")
        return self


class CarryMarketDay(FrozenModel):
    symbol: str
    open_time: datetime
    close_time: datetime
    contract_open: Decimal = Field(gt=0)
    contract_high: Decimal = Field(gt=0)
    contract_low: Decimal = Field(gt=0)
    contract_close: Decimal = Field(gt=0)
    mark_open: Decimal = Field(gt=0)
    mark_high: Decimal = Field(gt=0)
    mark_low: Decimal = Field(gt=0)
    mark_close: Decimal = Field(gt=0)
    index_open: Decimal = Field(gt=0)
    index_high: Decimal = Field(gt=0)
    index_low: Decimal = Field(gt=0)
    index_close: Decimal = Field(gt=0)
    premium_open: Decimal
    premium_high: Decimal
    premium_low: Decimal
    premium_close: Decimal

    _utc_open_time = field_validator("open_time")(_require_utc)
    _utc_close_time = field_validator("close_time")(_require_utc)

    @model_validator(mode="after")
    def candle_bounds_are_valid(self):
        if self.close_time <= self.open_time:
            raise ValueError("carry 日线收盘时间必须晚于开盘时间")
        for prefix in ("contract", "mark", "index", "premium"):
            open_price = getattr(self, f"{prefix}_open")
            high = getattr(self, f"{prefix}_high")
            low = getattr(self, f"{prefix}_low")
            close = getattr(self, f"{prefix}_close")
            if high < max(open_price, close) or low > min(open_price, close) or high < low:
                raise ValueError(f"{prefix} OHLC 边界非法")
        return self


class CarryFundingSettlement(FrozenModel):
    symbol: str
    funding_time: datetime
    available_at: datetime
    funding_interval_hours: int = Field(gt=0, le=24)
    funding_rate: Decimal
    mark_price: Decimal = Field(gt=0)

    _utc_funding_time = field_validator("funding_time")(_require_utc)
    _utc_available_at = field_validator("available_at")(_require_utc)

    @model_validator(mode="after")
    def availability_follows_settlement(self):
        if self.available_at <= self.funding_time:
            raise ValueError("carry 资金结算只能在发生后可见")
        return self


class HistoricalCarryDatasetManifest(FrozenModel):
    schema_version: Literal["historical-binance-carry-v1"] = (
        "historical-binance-carry-v1"
    )
    dataset_id: str
    source: Literal["binance-usdm-rest-carry"] = "binance-usdm-rest-carry"
    symbol: str
    interval: Literal["1d"] = "1d"
    collected_at: datetime
    requested_start: datetime
    requested_end: datetime
    spot_dataset_id: str
    funding_dataset_id: str
    first_open_time: datetime
    last_close_time: datetime
    first_funding_time: datetime
    last_funding_time: datetime
    day_count: int = Field(gt=0)
    settlement_count: int = Field(gt=0)
    days_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    settlements_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    settlement_mark_method: Literal["MARK_8H_PRE_SETTLEMENT_CLOSE"] = (
        "MARK_8H_PRE_SETTLEMENT_CLOSE"
    )
    instrument: CarryInstrumentSpec

    _utc_collected_at = field_validator("collected_at")(_require_utc)
    _utc_requested_start = field_validator("requested_start")(_require_utc)
    _utc_requested_end = field_validator("requested_end")(_require_utc)
    _utc_first_open = field_validator("first_open_time")(_require_utc)
    _utc_last_close = field_validator("last_close_time")(_require_utc)
    _utc_first_funding = field_validator("first_funding_time")(_require_utc)
    _utc_last_funding = field_validator("last_funding_time")(_require_utc)

    @model_validator(mode="after")
    def identity_and_bounds_match(self):
        if not self.requested_start < self.requested_end <= self.collected_at:
            raise ValueError("carry 数据请求窗口或冻结时间非法")
        if not (
            self.requested_start <= self.first_open_time
            < self.last_close_time
            < self.requested_end
        ):
            raise ValueError("carry 日线边界与请求窗口不一致")
        if not (
            self.requested_start <= self.first_funding_time
            <= self.last_funding_time
            < self.requested_end
        ):
            raise ValueError("carry 资金结算边界与请求窗口不一致")
        expected = stable_id(
            "historical_carry_dataset",
            self.schema_version,
            self.source,
            self.symbol,
            self.interval,
            self.requested_start,
            self.requested_end,
            self.spot_dataset_id,
            self.funding_dataset_id,
            self.days_hash,
            self.settlements_hash,
            self.settlement_mark_method,
            self.instrument,
        )
        if self.dataset_id != expected:
            raise ValueError("carry 数据集 ID 与冻结内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class HistoricalCarryDataset:
    manifest: HistoricalCarryDatasetManifest
    days: tuple[CarryMarketDay, ...]
    settlements: tuple[CarryFundingSettlement, ...]

    def __post_init__(self) -> None:
        _validate_carry_dataset(self)


class HistoricalCarryDatasetCatalog:
    """Content-addressed derivative facts; referenced spot/funding facts stay separate."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, dataset: HistoricalCarryDataset) -> Path:
        target = self._root / dataset.manifest.dataset_id
        if target.exists():
            existing = self.load(dataset.manifest.dataset_id)
            same_manifest_identity = existing.manifest.model_copy(
                update={"collected_at": dataset.manifest.collected_at}
            ) == dataset.manifest
            if (
                not same_manifest_identity
                or existing.days != dataset.days
                or existing.settlements != dataset.settlements
            ):
                raise ValueError("同一 carry 数据集 ID 的内容不一致")
            return target

        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".carry-dataset-", dir=self._root))
        try:
            _write_json(temporary / "days.json", [_compact_day(item) for item in dataset.days])
            _write_json(
                temporary / "settlements.json",
                [_compact_settlement(item) for item in dataset.settlements],
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

    def load(self, dataset_id: str) -> HistoricalCarryDataset:
        target = self._root / dataset_id
        manifest = self.load_manifest(dataset_id)
        raw_days = json.loads((target / "days.json").read_text(encoding="utf-8"))
        raw_settlements = json.loads(
            (target / "settlements.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw_days, list) or not isinstance(raw_settlements, list):
            raise ValueError("carry 数据文件根节点必须是数组")
        return HistoricalCarryDataset(
            manifest=manifest,
            days=tuple(_day_from_compact(item, manifest.symbol) for item in raw_days),
            settlements=tuple(
                _settlement_from_compact(item, manifest.symbol)
                for item in raw_settlements
            ),
        )

    def load_manifest(self, dataset_id: str) -> HistoricalCarryDatasetManifest:
        return HistoricalCarryDatasetManifest.model_validate(
            json.loads(
                (self._root / dataset_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        )


async def fetch_binance_carry_history(
    *,
    base_url: str,
    spot_dataset: HistoricalDataset,
    funding_dataset: HistoricalFundingDataset,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalCarryDataset:
    """Freeze USD-M trade/mark/index/premium bars and settlement mark prices."""

    if base_url.rstrip("/") != _BINANCE_USDM_REST:
        raise ValueError("carry 历史只接受 Binance USD-M 官方 REST")
    spot = spot_dataset.manifest
    funding = funding_dataset.manifest
    if (
        spot.interval != "1d"
        or spot.symbol != funding.symbol
        or spot.requested_start != funding.requested_start
        or spot.requested_end != funding.requested_end
    ):
        raise ValueError("carry 现货与资金费率数据必须同品种、同窗口且为日线")
    collected_at = _require_utc((clock or (lambda: datetime.now(UTC)))())
    if spot.requested_end > collected_at:
        raise ValueError("carry 数据窗口终点不能晚于冻结时间")

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    ) as client:
        instrument_response = await client.get(
            "/fapi/v1/exchangeInfo", params={"symbol": spot.symbol}
        )
        instrument_response.raise_for_status()
        instrument = _parse_carry_instrument(instrument_response.json(), spot.symbol)
        series = {}
        for name, endpoint, parameter in (
            ("contract", "/fapi/v1/klines", "symbol"),
            ("mark", "/fapi/v1/markPriceKlines", "symbol"),
            ("index", "/fapi/v1/indexPriceKlines", "pair"),
            ("premium", "/fapi/v1/premiumIndexKlines", "symbol"),
        ):
            series[name] = await _fetch_kline_series(
                client,
                endpoint=endpoint,
                parameter=parameter,
                symbol=spot.symbol,
                interval="1d",
                interval_milliseconds=_DAY_MILLISECONDS,
                start=spot.requested_start,
                end=spot.requested_end,
                collected_at=collected_at,
            )
        settlement_marks = await _fetch_kline_series(
            client,
            endpoint="/fapi/v1/markPriceKlines",
            parameter="symbol",
            symbol=spot.symbol,
            interval="8h",
            interval_milliseconds=8 * 60 * 60 * 1000,
            start=spot.requested_start - timedelta(hours=8),
            end=spot.requested_end,
            collected_at=collected_at,
        )
        raw_settlements = await _fetch_funding_settlements(
            client,
            symbol=spot.symbol,
            start=spot.requested_start,
            end=spot.requested_end,
        )

    spot_times = tuple(item.open_time for item in spot_dataset.bars)
    if any(tuple(item[0] for item in series[name]) != spot_times for name in series):
        raise ValueError("carry 衍生日线没有与冻结现货逐日完整对齐")
    days = tuple(
        CarryMarketDay(
            symbol=spot.symbol,
            open_time=series["contract"][index][0],
            close_time=series["contract"][index][1],
            **{
                f"{name}_{field}": series[name][index][offset]
                for name in series
                for field, offset in (("open", 2), ("high", 3), ("low", 4), ("close", 5))
            },
        )
        for index in range(len(spot_times))
    )
    settlements = _match_funding_settlements(
        funding_dataset,
        raw_settlements,
        settlement_marks,
    )
    days_hash = _days_hash(days)
    settlements_hash = _settlements_hash(settlements)
    identity = (
        "historical-binance-carry-v1",
        "binance-usdm-rest-carry",
        spot.symbol,
        "1d",
        spot.requested_start,
        spot.requested_end,
        spot.dataset_id,
        funding.dataset_id,
        days_hash,
        settlements_hash,
        "MARK_8H_PRE_SETTLEMENT_CLOSE",
        instrument,
    )
    manifest = HistoricalCarryDatasetManifest(
        dataset_id=stable_id("historical_carry_dataset", *identity),
        symbol=spot.symbol,
        collected_at=collected_at,
        requested_start=spot.requested_start,
        requested_end=spot.requested_end,
        spot_dataset_id=spot.dataset_id,
        funding_dataset_id=funding.dataset_id,
        first_open_time=days[0].open_time,
        last_close_time=days[-1].close_time,
        first_funding_time=settlements[0].funding_time,
        last_funding_time=settlements[-1].funding_time,
        day_count=len(days),
        settlement_count=len(settlements),
        days_hash=days_hash,
        settlements_hash=settlements_hash,
        instrument=instrument,
    )
    return HistoricalCarryDataset(
        manifest=manifest,
        days=days,
        settlements=settlements,
    )


async def _fetch_kline_series(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    parameter: str,
    symbol: str,
    interval: str,
    interval_milliseconds: int,
    start: datetime,
    end: datetime,
    collected_at: datetime,
) -> tuple[tuple[datetime, datetime, Decimal, Decimal, Decimal, Decimal], ...]:
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    collected_ms = int(collected_at.timestamp() * 1000)
    rows: list[tuple[datetime, datetime, Decimal, Decimal, Decimal, Decimal]] = []
    while cursor < end_ms:
        response = await client.get(
            endpoint,
            params={
                parameter: symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 1500,
            },
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError(f"Binance carry REST 响应非法: {endpoint}")
        if not page:
            break
        for item in page:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError(f"Binance carry K 线条目非法: {endpoint}")
            open_ms = int(item[0])
            close_ms = int(item[6])
            if open_ms < cursor or open_ms >= end_ms or close_ms > collected_ms:
                continue
            rows.append(
                (
                    datetime.fromtimestamp(open_ms / 1000, tz=UTC),
                    datetime.fromtimestamp(close_ms / 1000, tz=UTC),
                    Decimal(str(item[1])),
                    Decimal(str(item[2])),
                    Decimal(str(item[3])),
                    Decimal(str(item[4])),
                )
            )
        next_cursor = int(page[-1][0]) + interval_milliseconds
        if next_cursor <= cursor:
            raise ValueError(f"Binance carry K 线分页未前进: {endpoint}")
        cursor = next_cursor
    if not rows:
        raise ValueError(f"Binance carry K 线为空: {endpoint}")
    return tuple(rows)


async def _fetch_funding_settlements(
    client: httpx.AsyncClient,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[tuple[int, Decimal], ...]:
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: list[tuple[int, Decimal]] = []
    while cursor < end_ms:
        response = await client.get(
            "/fapi/v1/fundingRate",
            params={
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 1000,
            },
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError("Binance fundingRate REST 响应非法")
        if not page:
            break
        for item in page:
            if not isinstance(item, dict):
                raise ValueError("Binance fundingRate REST 条目非法")
            try:
                funding_ms = int(item["fundingTime"])
                rate = Decimal(str(item["fundingRate"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Binance fundingRate REST 条目缺字段") from exc
            if cursor <= funding_ms < end_ms:
                rows.append((funding_ms, rate))
        next_cursor = int(page[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            raise ValueError("Binance fundingRate REST 分页未前进")
        cursor = next_cursor
    if not rows:
        raise ValueError("Binance carry 资金结算价为空")
    ordered = tuple(sorted(set(rows), key=lambda item: item[0]))
    if len(ordered) != len(rows):
        raise ValueError("Binance fundingRate REST 返回重复结算")
    return ordered


def _match_funding_settlements(
    funding_dataset: HistoricalFundingDataset,
    raw: tuple[tuple[int, Decimal], ...],
    settlement_marks: tuple[
        tuple[datetime, datetime, Decimal, Decimal, Decimal, Decimal], ...
    ],
) -> tuple[CarryFundingSettlement, ...]:
    if len(raw) != len(funding_dataset.observations):
        raise ValueError("Binance REST 与官方月档资金结算数量不一致")
    matched: list[CarryFundingSettlement] = []
    mark_by_close_ms = {
        int(item[1].timestamp() * 1000): item[5] for item in settlement_marks
    }
    if len(mark_by_close_ms) != len(settlement_marks):
        raise ValueError("Binance 8h 标记价收盘时间重复")
    mark_close_times = tuple(sorted(mark_by_close_ms))
    for observation, item in zip(funding_dataset.observations, raw, strict=True):
        canonical_ms = int(observation.funding_time.timestamp() * 1000)
        if (
            abs(item[0] - canonical_ms) > _FUNDING_TIME_TOLERANCE_MILLISECONDS
            or item[1] != observation.funding_rate
        ):
            raise ValueError("Binance REST 结算价没有与官方校验资金费率唯一匹配")
        mark_index = bisect_left(mark_close_times, canonical_ms) - 1
        if mark_index < 0:
            raise ValueError("Binance 8h 标记价没有唯一覆盖资金结算前一时刻")
        mark_close_ms = mark_close_times[mark_index]
        if canonical_ms - mark_close_ms > _FUNDING_TIME_TOLERANCE_MILLISECONDS:
            raise ValueError("Binance 8h 标记价没有唯一覆盖资金结算前一时刻")
        matched.append(
            CarryFundingSettlement(
                symbol=observation.symbol,
                funding_time=observation.funding_time,
                available_at=observation.available_at,
                funding_interval_hours=observation.funding_interval_hours,
                funding_rate=observation.funding_rate,
                mark_price=mark_by_close_ms[mark_close_ms],
            )
        )
    return tuple(matched)


def _parse_carry_instrument(raw: Any, symbol: str) -> CarryInstrumentSpec:
    if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
        raise ValueError("Binance USD-M exchangeInfo 响应非法")
    records = [item for item in raw["symbols"] if item.get("symbol") == symbol]
    if len(records) != 1:
        raise ValueError(f"Binance USD-M exchangeInfo 未唯一返回 {symbol}")
    record = records[0]
    filters = {
        item.get("filterType"): item
        for item in record.get("filters", [])
        if isinstance(item, dict) and isinstance(item.get("filterType"), str)
    }
    try:
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        notional = filters["MIN_NOTIONAL"]
        return CarryInstrumentSpec(
            symbol=symbol,
            pair=str(record["pair"]),
            contract_type=str(record["contractType"]),
            status=str(record["status"]),
            base_asset=str(record["baseAsset"]),
            quote_asset=str(record["quoteAsset"]),
            margin_asset=str(record["marginAsset"]),
            onboarded_at=datetime.fromtimestamp(int(record["onboardDate"]) / 1000, tz=UTC),
            price_increment=Decimal(str(price["tickSize"])),
            quantity_increment=Decimal(str(lot["stepSize"])),
            minimum_quantity=Decimal(str(lot["minQty"])),
            maximum_quantity=Decimal(str(lot["maxQty"])),
            minimum_notional=Decimal(str(notional["notional"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Binance USD-M exchangeInfo 缺少必要永续规则") from exc


def _validate_carry_dataset(dataset: HistoricalCarryDataset) -> None:
    manifest = dataset.manifest
    if (
        len(dataset.days) != manifest.day_count
        or len(dataset.settlements) != manifest.settlement_count
    ):
        raise ValueError("carry 数据数量与 Manifest 不一致")
    if not dataset.days or not dataset.settlements:
        raise ValueError("carry 数据集不能为空")
    if tuple(item.open_time for item in dataset.days) != tuple(
        sorted({item.open_time for item in dataset.days})
    ):
        raise ValueError("carry 日线必须严格递增且不重复")
    for previous, current in zip(dataset.days, dataset.days[1:], strict=False):
        if current.open_time.timestamp() - previous.open_time.timestamp() != 86_400:
            raise ValueError("carry 日线存在缺口")
    if any(item.symbol != manifest.symbol for item in dataset.days + dataset.settlements):
        raise ValueError("carry 数据品种与 Manifest 不一致")
    if (
        dataset.days[0].open_time != manifest.first_open_time
        or dataset.days[-1].close_time != manifest.last_close_time
        or dataset.settlements[0].funding_time != manifest.first_funding_time
        or dataset.settlements[-1].funding_time != manifest.last_funding_time
    ):
        raise ValueError("carry 数据边界与 Manifest 不一致")
    if _days_hash(dataset.days) != manifest.days_hash:
        raise ValueError("carry 日线内容哈希与 Manifest 不一致")
    if _settlements_hash(dataset.settlements) != manifest.settlements_hash:
        raise ValueError("carry 结算内容哈希与 Manifest 不一致")


def _compact_day(day: CarryMarketDay) -> list[Any]:
    return [
        int(day.open_time.timestamp() * 1000),
        int(day.close_time.timestamp() * 1000),
        *(
            str(getattr(day, f"{prefix}_{field}"))
            for prefix in ("contract", "mark", "index", "premium")
            for field in ("open", "high", "low", "close")
        ),
    ]


def _day_from_compact(raw: Any, symbol: str) -> CarryMarketDay:
    if not isinstance(raw, list) or len(raw) != 18:
        raise ValueError("carry 日线条目必须包含 18 个字段")
    values = iter(raw[2:])
    prices = {
        f"{prefix}_{field}": Decimal(str(next(values)))
        for prefix in ("contract", "mark", "index", "premium")
        for field in ("open", "high", "low", "close")
    }
    return CarryMarketDay(
        symbol=symbol,
        open_time=datetime.fromtimestamp(int(raw[0]) / 1000, tz=UTC),
        close_time=datetime.fromtimestamp(int(raw[1]) / 1000, tz=UTC),
        **prices,
    )


def _compact_settlement(settlement: CarryFundingSettlement) -> list[Any]:
    return [
        int(settlement.funding_time.timestamp() * 1000),
        int(settlement.available_at.timestamp() * 1000),
        settlement.funding_interval_hours,
        str(settlement.funding_rate),
        str(settlement.mark_price),
    ]


def _settlement_from_compact(raw: Any, symbol: str) -> CarryFundingSettlement:
    if not isinstance(raw, list) or len(raw) != 5:
        raise ValueError("carry 资金结算条目必须包含 5 个字段")
    return CarryFundingSettlement(
        symbol=symbol,
        funding_time=datetime.fromtimestamp(int(raw[0]) / 1000, tz=UTC),
        available_at=datetime.fromtimestamp(int(raw[1]) / 1000, tz=UTC),
        funding_interval_hours=int(raw[2]),
        funding_rate=Decimal(str(raw[3])),
        mark_price=Decimal(str(raw[4])),
    )


def _hash_compact(rows: Iterable[list[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _days_hash(days: Iterable[CarryMarketDay]) -> str:
    return _hash_compact(_compact_day(item) for item in days)


def _settlements_hash(settlements: Iterable[CarryFundingSettlement]) -> str:
    return _hash_compact(_compact_settlement(item) for item in settlements)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
