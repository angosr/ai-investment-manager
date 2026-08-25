from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from sqlalchemy import Engine, Select, select

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.tables import market_quotes, perpetual_quotes


class HistoricalExecutableQuote(FrozenModel):
    source_quote_id: str = Field(min_length=1)
    observed_at: datetime
    exchange_time: datetime | None = None
    bid: PositiveDecimal
    bid_quantity: Money
    ask: PositiveDecimal
    ask_quantity: Money
    source: str = Field(min_length=1)

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_exchange_time = field_validator("exchange_time")(
        lambda value: require_utc(value) if value else None
    )

    @model_validator(mode="after")
    def prices_and_visibility_match(self):
        if self.ask < self.bid:
            raise ValueError("历史可执行报价 ask 不能低于 bid")
        if self.bid_quantity <= 0 or self.ask_quantity <= 0:
            raise ValueError("历史可执行报价双边数量必须为正")
        if self.exchange_time is not None and self.exchange_time > self.observed_at:
            raise ValueError("历史可执行报价交易所时间不能晚于本地可见时间")
        return self


class HistoricalExecutableQuoteManifest(FrozenModel):
    schema_version: str = "historical-executable-quotes-v1"
    dataset_id: str
    instrument: InstrumentId
    source_table: str
    captured_at: datetime
    requested_start: datetime
    requested_end: datetime
    sampling_interval_seconds: int = Field(gt=0)
    source_row_count: int = Field(gt=0)
    quote_count: int = Field(gt=1)
    first_observed_at: datetime
    last_observed_at: datetime
    quotes_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_captured_at = field_validator("captured_at")(require_utc)
    _utc_requested_start = field_validator("requested_start")(require_utc)
    _utc_requested_end = field_validator("requested_end")(require_utc)
    _utc_first_observed_at = field_validator("first_observed_at")(require_utc)
    _utc_last_observed_at = field_validator("last_observed_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_bounds_match(self):
        if not (
            self.requested_start
            <= self.first_observed_at
            < self.last_observed_at
            < self.requested_end
            <= self.captured_at
        ):
            raise ValueError("历史可执行报价窗口或采集时间非法")
        if self.quote_count > self.source_row_count:
            raise ValueError("历史可执行报价样本数不能超过来源行数")
        expected = stable_id(
            "historical_executable_quotes",
            self.schema_version,
            self.instrument,
            self.source_table,
            self.requested_start,
            self.requested_end,
            self.sampling_interval_seconds,
            self.source_row_count,
            self.quote_count,
            self.first_observed_at,
            self.last_observed_at,
            self.quotes_hash,
        )
        if self.dataset_id != expected:
            raise ValueError("历史可执行报价数据集 ID 与冻结内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class HistoricalExecutableQuoteDataset:
    manifest: HistoricalExecutableQuoteManifest
    quotes: tuple[HistoricalExecutableQuote, ...]

    def __post_init__(self) -> None:
        manifest = self.manifest
        if len(self.quotes) != manifest.quote_count:
            raise ValueError("历史可执行报价数量与 Manifest 不一致")
        times = tuple(item.observed_at for item in self.quotes)
        if times != tuple(sorted(set(times))):
            raise ValueError("历史可执行报价时间必须唯一且递增")
        if times[0] != manifest.first_observed_at or times[-1] != manifest.last_observed_at:
            raise ValueError("历史可执行报价范围与 Manifest 不一致")
        if _quotes_hash(self.quotes) != manifest.quotes_hash:
            raise ValueError("历史可执行报价哈希与 Manifest 不一致")


class HistoricalExecutableQuoteCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, dataset: HistoricalExecutableQuoteDataset) -> Path:
        target = self._root / dataset.manifest.dataset_id
        if target.exists():
            if self.load(dataset.manifest.dataset_id) != dataset:
                raise ValueError("同一历史可执行报价数据集 ID 的内容不一致")
            return target
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".quote-dataset-", dir=self._root))
        try:
            _write_json(
                temporary / "quotes.json",
                [_compact_quote(item) for item in dataset.quotes],
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

    def load(self, dataset_id: str) -> HistoricalExecutableQuoteDataset:
        target = self._root / dataset_id
        manifest = HistoricalExecutableQuoteManifest.model_validate_json(
            (target / "manifest.json").read_text(encoding="utf-8")
        )
        raw = json.loads((target / "quotes.json").read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("历史可执行报价 quotes.json 根节点必须是数组")
        quotes = tuple(_quote_from_compact(item) for item in raw)
        return HistoricalExecutableQuoteDataset(manifest=manifest, quotes=quotes)


def freeze_executable_quotes(
    engine: Engine,
    *,
    instrument: InstrumentId,
    start: datetime,
    end: datetime,
    sampling_interval_seconds: int,
    captured_at: datetime | None = None,
) -> HistoricalExecutableQuoteDataset:
    """Freeze one quote per UTC bucket from the append-only production facts."""

    start = require_utc(start)
    end = require_utc(end)
    captured_at = require_utc(captured_at or datetime.now(UTC))
    if start >= end or end > captured_at:
        raise ValueError("历史可执行报价请求窗口非法")
    if sampling_interval_seconds <= 0:
        raise ValueError("历史可执行报价采样间隔必须为正")
    statement, source_table = _quote_statement(
        instrument=instrument,
        start=start,
        end=end,
    )
    quotes: list[HistoricalExecutableQuote] = []
    source_row_count = 0
    previous_bucket: int | None = None
    with engine.connect().execution_options(stream_results=True) as connection:
        for row in connection.execute(statement).mappings():
            source_row_count += 1
            quote = _quote_from_row(row, instrument=instrument)
            bucket = int(quote.observed_at.timestamp()) // sampling_interval_seconds
            if bucket == previous_bucket:
                continue
            quotes.append(quote)
            previous_bucket = bucket
    if len(quotes) < 2:
        raise ValueError("指定窗口缺少至少两个历史可执行报价样本")
    frozen = tuple(quotes)
    quotes_hash = _quotes_hash(frozen)
    values = {
        "schema_version": "historical-executable-quotes-v1",
        "instrument": instrument,
        "source_table": source_table,
        "requested_start": start,
        "requested_end": end,
        "sampling_interval_seconds": sampling_interval_seconds,
        "source_row_count": source_row_count,
        "quote_count": len(frozen),
        "first_observed_at": frozen[0].observed_at,
        "last_observed_at": frozen[-1].observed_at,
        "quotes_hash": quotes_hash,
    }
    manifest = HistoricalExecutableQuoteManifest(
        dataset_id=stable_id("historical_executable_quotes", *values.values()),
        captured_at=captured_at,
        **values,
    )
    return HistoricalExecutableQuoteDataset(manifest=manifest, quotes=frozen)


def _quote_statement(
    *,
    instrument: InstrumentId,
    start: datetime,
    end: datetime,
) -> tuple[Select, str]:
    if instrument.product == InstrumentProduct.SPOT:
        return (
            select(
                market_quotes.c.quote_id,
                market_quotes.c.observed_at,
                market_quotes.c.payload,
            )
            .where(
                market_quotes.c.symbol == instrument.symbol,
                market_quotes.c.observed_at >= start,
                market_quotes.c.observed_at < end,
            )
            .order_by(market_quotes.c.observed_at, market_quotes.c.quote_id),
            market_quotes.name,
        )
    return (
        select(
            perpetual_quotes.c.quote_id,
            perpetual_quotes.c.exchange_time,
            perpetual_quotes.c.observed_at,
            perpetual_quotes.c.payload,
        )
        .where(
            perpetual_quotes.c.instrument_id == instrument.key,
            perpetual_quotes.c.observed_at >= start,
            perpetual_quotes.c.observed_at < end,
        )
        .order_by(perpetual_quotes.c.observed_at, perpetual_quotes.c.quote_id),
        perpetual_quotes.name,
    )


def _quote_from_row(row, *, instrument: InstrumentId) -> HistoricalExecutableQuote:
    payload = row["payload"]
    if not isinstance(payload, dict):
        raise ValueError("历史可执行报价来源 payload 非法")
    if str(payload.get("quote_id")) != row["quote_id"]:
        raise ValueError("历史可执行报价行身份与 payload 不一致")
    observed_at = _utc_from_storage(row["observed_at"])
    if _utc_from_iso(payload.get("observed_at")) != observed_at:
        raise ValueError("历史可执行报价行时间与 payload 不一致")
    exchange_time = None
    if instrument.product != InstrumentProduct.SPOT:
        exchange_time = _utc_from_storage(row["exchange_time"])
        if _utc_from_iso(payload.get("exchange_time")) != exchange_time:
            raise ValueError("历史可执行报价交易所时间与 payload 不一致")
        raw_instrument = payload.get("instrument")
        if InstrumentId.model_validate(raw_instrument) != instrument:
            raise ValueError("历史可执行报价 Instrument 与请求不一致")
    elif str(payload.get("symbol")) != instrument.symbol:
        raise ValueError("历史可执行现货报价 symbol 与请求不一致")
    try:
        return HistoricalExecutableQuote(
            source_quote_id=row["quote_id"],
            observed_at=observed_at,
            exchange_time=exchange_time,
            bid=payload["bid"],
            bid_quantity=payload["bid_quantity"],
            ask=payload["ask"],
            ask_quantity=payload["ask_quantity"],
            source=payload["source"],
        )
    except KeyError as exc:
        raise ValueError("历史可执行报价 payload 缺少必要字段") from exc


def _utc_from_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("历史可执行报价 ISO 时间非法")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("历史可执行报价 ISO 时间非法") from exc
    return require_utc(parsed)


def _utc_from_storage(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("历史可执行报价数据库时间非法")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return require_utc(value)


def _compact_quote(item: HistoricalExecutableQuote) -> list[str | None]:
    return [
        item.source_quote_id,
        item.observed_at.isoformat(),
        item.exchange_time.isoformat() if item.exchange_time else None,
        str(item.bid),
        str(item.bid_quantity),
        str(item.ask),
        str(item.ask_quantity),
        item.source,
    ]


def _quote_from_compact(raw: object) -> HistoricalExecutableQuote:
    if not isinstance(raw, list) or len(raw) != 8:
        raise ValueError("历史可执行报价条目必须包含 8 个字段")
    try:
        return HistoricalExecutableQuote(
            source_quote_id=raw[0],
            observed_at=datetime.fromisoformat(raw[1]),
            exchange_time=datetime.fromisoformat(raw[2]) if raw[2] else None,
            bid=raw[3],
            bid_quantity=raw[4],
            ask=raw[5],
            ask_quantity=raw[6],
            source=raw[7],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("历史可执行报价条目非法") from exc


def _quotes_hash(quotes: tuple[HistoricalExecutableQuote, ...]) -> str:
    return content_hash([_compact_quote(item) for item in quotes])


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
