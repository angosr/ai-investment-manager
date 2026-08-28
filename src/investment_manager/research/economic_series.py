from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import httpx
from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.portfolio.policy import EconomicExposure

FAMA_FRENCH_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)
FAMA_FRENCH_DOCUMENTATION_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
    "Data_Library/f-f_factors.html"
)
WORLD_BANK_COMMODITY_PAGE_URL = (
    "https://www.worldbank.org/en/research/commodity-markets"
)
FRED_CPI_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
FRED_CPI_DOCUMENTATION_URL = "https://fred.stlouisfed.org/series/CPIAUCSL"

_FAMA_MEMBER = "F-F_Research_Data_Factors_daily.csv"
_WORLD_BANK_MONTHLY_FILE = re.compile(
    r"https://thedocs\.worldbank\.org/[^\"'<> ]+/CMO-Historical-Data-Monthly\.xlsx"
)
_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIP_NS = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
)
_PACKAGE_RELATIONSHIP_NS = (
    "{http://schemas.openxmlformats.org/package/2006/relationships}"
)


class EconomicSeriesKind(StrEnum):
    TOTAL_RETURN = "TOTAL_RETURN"
    PRICE_LEVEL = "PRICE_LEVEL"


class EconomicSeriesRole(StrEnum):
    EXPOSURE_PROXY = "EXPOSURE_PROXY"
    OBJECTIVE_DEFLATOR = "OBJECTIVE_DEFLATOR"


class EconomicSeriesFrequency(StrEnum):
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"


class EconomicSeriesVintagePolicy(StrEnum):
    CURRENT_VINTAGE_AT_COLLECTION = "CURRENT_VINTAGE_AT_COLLECTION"


class EconomicSeriesObservation(FrozenModel):
    effective_date: date
    value: Decimal


class HistoricalEconomicSeriesManifest(FrozenModel):
    """One present-day historical view; it cannot prove past data availability."""

    schema_version: str = "historical-economic-series-v1"
    dataset_id: str
    series_id: str = Field(min_length=1)
    role: EconomicSeriesRole
    economic_exposure: EconomicExposure | None = None
    kind: EconomicSeriesKind
    frequency: EconomicSeriesFrequency
    unit: str = Field(min_length=1)
    vintage_policy: EconomicSeriesVintagePolicy
    source_name: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https://")
    documentation_url: str = Field(pattern=r"^https://")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collected_at: datetime
    first_effective_date: date
    last_effective_date: date
    observation_count: int = Field(gt=1)
    observations_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_collected_at = field_validator("collected_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_bounds_match(self):
        if (self.role == EconomicSeriesRole.EXPOSURE_PROXY) != (
            self.economic_exposure is not None
        ):
            raise ValueError("经济代理角色与经济暴露必须同时存在")
        if self.first_effective_date >= self.last_effective_date:
            raise ValueError("经济代理数据日期范围非法")
        if self.last_effective_date > self.collected_at.date():
            raise ValueError("经济代理数据包含采集时尚未发生的观测")
        expected = stable_id(
            "historical_economic_series",
            self.schema_version,
            self.series_id,
            self.role,
            self.economic_exposure,
            self.kind,
            self.frequency,
            self.unit,
            self.vintage_policy,
            self.source_name,
            self.source_url,
            self.documentation_url,
            self.source_sha256,
            self.first_effective_date,
            self.last_effective_date,
            self.observation_count,
            self.observations_hash,
        )
        if self.dataset_id != expected:
            raise ValueError("经济代理数据集 ID 与冻结内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class HistoricalEconomicSeriesDataset:
    manifest: HistoricalEconomicSeriesManifest
    observations: tuple[EconomicSeriesObservation, ...]

    def __post_init__(self) -> None:
        _validate_dataset(self)


class HistoricalEconomicSeriesCatalog:
    """Content-addressed current-vintage proxies, separate from product history."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, dataset: HistoricalEconomicSeriesDataset) -> Path:
        target = self._root / dataset.manifest.dataset_id
        if target.exists():
            if self.load(dataset.manifest.dataset_id) != dataset:
                raise ValueError("同一经济代理数据集 ID 的内容不一致")
            return target
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".economic-series-", dir=self._root))
        try:
            _write_json(
                temporary / "observations.json",
                [
                    [item.effective_date.isoformat(), str(item.value)]
                    for item in dataset.observations
                ],
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

    def load(self, dataset_id: str) -> HistoricalEconomicSeriesDataset:
        target = self._root / dataset_id
        manifest = HistoricalEconomicSeriesManifest.model_validate_json(
            (target / "manifest.json").read_text(encoding="utf-8")
        )
        raw = json.loads((target / "observations.json").read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("经济代理 observations.json 根节点必须是数组")
        try:
            observations = tuple(
                EconomicSeriesObservation(effective_date=date.fromisoformat(row[0]), value=row[1])
                for row in raw
                if isinstance(row, list) and len(row) == 2
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("经济代理观测条目非法") from exc
        if len(observations) != len(raw):
            raise ValueError("经济代理观测条目必须包含日期和值")
        return HistoricalEconomicSeriesDataset(
            manifest=manifest,
            observations=observations,
        )


async def fetch_fama_french_us_market_returns(
    *,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalEconomicSeriesDataset:
    """Freeze the current CRSP US market total-return history published by French."""

    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
    payload = await _download(
        FAMA_FRENCH_DAILY_URL,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    rows = _parse_fama_french_daily(payload)
    observations = tuple(
        EconomicSeriesObservation(effective_date=item[0], value=item[1])
        for item in rows
    )
    return _build_dataset(
        series_id="US_CRSP_VALUE_WEIGHTED_MARKET_TOTAL_RETURN",
        role=EconomicSeriesRole.EXPOSURE_PROXY,
        economic_exposure=EconomicExposure.US_EQUITY,
        kind=EconomicSeriesKind.TOTAL_RETURN,
        frequency=EconomicSeriesFrequency.DAILY,
        unit="DECIMAL_RETURN",
        source_name="Kenneth French Data Library / CRSP",
        source_url=FAMA_FRENCH_DAILY_URL,
        documentation_url=FAMA_FRENCH_DOCUMENTATION_URL,
        source_payload=payload,
        collected_at=collected_at,
        observations=observations,
    )


async def fetch_fama_french_us_one_month_tbill_returns(
    *,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalEconomicSeriesDataset:
    """Freeze the one-month Treasury-bill return carried in the same source."""

    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
    payload = await _download(
        FAMA_FRENCH_DAILY_URL,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    rows = _parse_fama_french_daily(payload)
    observations = tuple(
        EconomicSeriesObservation(effective_date=item[0], value=item[2])
        for item in rows
    )
    return _build_dataset(
        series_id="US_ONE_MONTH_TREASURY_BILL_TOTAL_RETURN",
        role=EconomicSeriesRole.EXPOSURE_PROXY,
        economic_exposure=EconomicExposure.CASH,
        kind=EconomicSeriesKind.TOTAL_RETURN,
        frequency=EconomicSeriesFrequency.DAILY,
        unit="DECIMAL_RETURN",
        source_name="Kenneth French Data Library / Ibbotson and ICE BofA",
        source_url=FAMA_FRENCH_DAILY_URL,
        documentation_url=FAMA_FRENCH_DOCUMENTATION_URL,
        source_payload=payload,
        collected_at=collected_at,
        observations=observations,
    )


async def fetch_world_bank_gold_prices(
    *,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalEconomicSeriesDataset:
    """Resolve and freeze the World Bank Pink Sheet monthly gold series."""

    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
    page = await _download(
        WORLD_BANK_COMMODITY_PAGE_URL,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    urls = tuple(sorted(set(_WORLD_BANK_MONTHLY_FILE.findall(html.unescape(page.decode())))))
    if len(urls) != 1:
        raise ValueError("World Bank 商品页未唯一指向月度历史数据")
    source_url = urls[0]
    payload = await _download(
        source_url,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    observations = _parse_world_bank_gold_monthly(payload)
    return _build_dataset(
        series_id="WORLD_BANK_GOLD_USD_MONTHLY_PRICE",
        role=EconomicSeriesRole.EXPOSURE_PROXY,
        economic_exposure=EconomicExposure.INFLATION_SENSITIVE,
        kind=EconomicSeriesKind.PRICE_LEVEL,
        frequency=EconomicSeriesFrequency.MONTHLY,
        unit="USD_PER_TROY_OUNCE",
        source_name="World Bank Commodity Price Data (Pink Sheet)",
        source_url=source_url,
        documentation_url=WORLD_BANK_COMMODITY_PAGE_URL,
        source_payload=payload,
        collected_at=collected_at,
        observations=observations,
    )


async def fetch_fred_us_cpi(
    *,
    timeout_seconds: int,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HistoricalEconomicSeriesDataset:
    """Freeze the current BLS CPI history hosted by the St. Louis Fed."""

    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
    payload = await _download(
        FRED_CPI_URL,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    observations = _parse_fred_cpi(payload)
    return _build_dataset(
        series_id="US_CPI_ALL_URBAN_CONSUMERS_SEASONALLY_ADJUSTED",
        role=EconomicSeriesRole.OBJECTIVE_DEFLATOR,
        economic_exposure=None,
        kind=EconomicSeriesKind.PRICE_LEVEL,
        frequency=EconomicSeriesFrequency.MONTHLY,
        unit="INDEX_1982_1984_EQUALS_100",
        source_name="U.S. Bureau of Labor Statistics via FRED",
        source_url=FRED_CPI_URL,
        documentation_url=FRED_CPI_DOCUMENTATION_URL,
        source_payload=payload,
        collected_at=collected_at,
        observations=observations,
    )


async def _download(
    url: str,
    *,
    timeout_seconds: int,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _parse_fama_french_daily(
    payload: bytes,
) -> tuple[tuple[date, Decimal, Decimal], ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist() != [_FAMA_MEMBER]:
                raise ValueError("Fama-French 压缩包成员与固定合同不一致")
            raw = archive.read(_FAMA_MEMBER).decode("utf-8-sig")
    except (UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Fama-French 历史文件编码或压缩结构非法") from exc

    rows = csv.reader(io.StringIO(raw))
    header_seen = False
    observations: list[tuple[date, Decimal, Decimal]] = []
    for row in rows:
        normalized = tuple(item.strip() for item in row)
        if not header_seen:
            if normalized == ("", "Mkt-RF", "SMB", "HML", "RF"):
                header_seen = True
            continue
        if not normalized or not re.fullmatch(r"[0-9]{8}", normalized[0]):
            if observations:
                break
            continue
        if len(normalized) != 5:
            raise ValueError("Fama-French 日频数据列数非法")
        try:
            effective_date = datetime.strptime(normalized[0], "%Y%m%d").date()
            risk_free_return = Decimal(normalized[4]) / Decimal("100")
            market_return = Decimal(normalized[1]) / Decimal("100") + risk_free_return
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Fama-French 日频收益条目非法") from exc
        observations.append((effective_date, market_return, risk_free_return))
    if not header_seen or len(observations) < 252:
        raise ValueError("Fama-French 日频市场收益历史不完整")
    return tuple(observations)


def _parse_world_bank_gold_monthly(
    payload: bytes,
) -> tuple[EconomicSeriesObservation, ...]:
    rows = _xlsx_rows(payload, sheet_name="Monthly Prices")
    if len(rows) < 12:
        raise ValueError("World Bank 月度价格表缺少历史观测")
    header = rows.get(5, {})
    gold_columns = tuple(key for key, value in header.items() if value == "Gold")
    if len(gold_columns) != 1:
        raise ValueError("World Bank 月度价格表未唯一包含 Gold 列")
    gold_column = gold_columns[0]
    if rows.get(6, {}).get(gold_column) != "($/troy oz)":
        raise ValueError("World Bank Gold 单位与固定合同不一致")
    observations: list[EconomicSeriesObservation] = []
    for row_number in sorted(key for key in rows if key >= 7):
        row = rows[row_number]
        period = row.get("A")
        raw_value = row.get(gold_column)
        if period is None or raw_value in {None, "", "…", ".."}:
            continue
        match = re.fullmatch(r"([0-9]{4})M(0[1-9]|1[0-2])", period)
        if match is None:
            raise ValueError("World Bank Gold 月份格式非法")
        try:
            value = Decimal(raw_value)
        except InvalidOperation as exc:
            raise ValueError("World Bank Gold 价格非法") from exc
        if value <= 0:
            raise ValueError("World Bank Gold 价格必须为正")
        observations.append(
            EconomicSeriesObservation(
                effective_date=date(int(match.group(1)), int(match.group(2)), 1),
                value=value,
            )
        )
    if len(observations) < 120:
        raise ValueError("World Bank Gold 月度历史长度不足")
    return tuple(observations)


def _parse_fred_cpi(payload: bytes) -> tuple[EconomicSeriesObservation, ...]:
    try:
        rows = csv.reader(io.StringIO(payload.decode("utf-8-sig")))
        header = next(rows)
    except (StopIteration, UnicodeDecodeError) as exc:
        raise ValueError("FRED CPI CSV 编码或结构非法") from exc
    if header != ["observation_date", "CPIAUCSL"]:
        raise ValueError("FRED CPI CSV 表头与固定合同不一致")
    observations: list[EconomicSeriesObservation] = []
    for row in rows:
        if len(row) != 2 or not row[1].strip() or row[1].strip() == ".":
            continue
        try:
            effective_date = date.fromisoformat(row[0].strip())
            value = Decimal(row[1].strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("FRED CPI 观测条目非法") from exc
        if effective_date.day != 1 or value <= 0:
            raise ValueError("FRED CPI 必须是正值月度指数")
        observations.append(
            EconomicSeriesObservation(effective_date=effective_date, value=value)
        )
    if len(observations) < 120:
        raise ValueError("FRED CPI 月度历史长度不足")
    return tuple(observations)


def _xlsx_rows(payload: bytes, *, sheet_name: str) -> dict[int, dict[str, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            shared_strings = _xlsx_shared_strings(archive)
            sheet_path = _xlsx_sheet_path(archive, sheet_name=sheet_name)
            root = ElementTree.fromstring(archive.read(sheet_path))
    except (ElementTree.ParseError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("XLSX 文件结构非法") from exc
    rows: dict[int, dict[str, str]] = {}
    for row in root.iter(f"{_SPREADSHEET_NS}row"):
        row_number = int(row.attrib["r"])
        cells: dict[str, str] = {}
        for cell in row.findall(f"{_SPREADSHEET_NS}c"):
            reference = cell.attrib.get("r", "")
            match = re.fullmatch(r"([A-Z]+)[0-9]+", reference)
            if match is None:
                raise ValueError("XLSX 单元格引用非法")
            value_node = cell.find(f"{_SPREADSHEET_NS}v")
            raw_value = "" if value_node is None else (value_node.text or "")
            if cell.attrib.get("t") == "s" and raw_value:
                try:
                    raw_value = shared_strings[int(raw_value)]
                except (IndexError, ValueError) as exc:
                    raise ValueError("XLSX 共享字符串引用非法") from exc
            cells[match.group(1)] = raw_value.strip()
        if cells:
            rows[row_number] = cells
    return rows


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return tuple(
        "".join(node.text or "" for node in item.iter(f"{_SPREADSHEET_NS}t"))
        for item in root.findall(f"{_SPREADSHEET_NS}si")
    )


def _xlsx_sheet_path(archive: zipfile.ZipFile, *, sheet_name: str) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    matches = tuple(
        sheet.attrib[f"{_RELATIONSHIP_NS}id"]
        for sheet in workbook.iter(f"{_SPREADSHEET_NS}sheet")
        if sheet.attrib.get("name") == sheet_name
    )
    if len(matches) != 1:
        raise ValueError(f"XLSX 未唯一包含工作表 {sheet_name}")
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    targets = tuple(
        item.attrib["Target"]
        for item in relationships.iter(f"{_PACKAGE_RELATIONSHIP_NS}Relationship")
        if item.attrib.get("Id") == matches[0]
    )
    if len(targets) != 1:
        raise ValueError("XLSX 工作表关系非法")
    target = PurePosixPath(targets[0])
    if target.is_absolute() or ".." in target.parts:
        raise ValueError("XLSX 工作表路径非法")
    return str(PurePosixPath("xl") / target)


def _build_dataset(
    *,
    series_id: str,
    role: EconomicSeriesRole,
    economic_exposure: EconomicExposure | None,
    kind: EconomicSeriesKind,
    frequency: EconomicSeriesFrequency,
    unit: str,
    source_name: str,
    source_url: str,
    documentation_url: str,
    source_payload: bytes,
    collected_at: datetime,
    observations: tuple[EconomicSeriesObservation, ...],
) -> HistoricalEconomicSeriesDataset:
    observations_hash = _observations_hash(observations)
    values = {
        "schema_version": "historical-economic-series-v1",
        "series_id": series_id,
        "role": role,
        "economic_exposure": economic_exposure,
        "kind": kind,
        "frequency": frequency,
        "unit": unit,
        "vintage_policy": EconomicSeriesVintagePolicy.CURRENT_VINTAGE_AT_COLLECTION,
        "source_name": source_name,
        "source_url": source_url,
        "documentation_url": documentation_url,
        "source_sha256": hashlib.sha256(source_payload).hexdigest(),
        "first_effective_date": observations[0].effective_date,
        "last_effective_date": observations[-1].effective_date,
        "observation_count": len(observations),
        "observations_hash": observations_hash,
    }
    manifest = HistoricalEconomicSeriesManifest(
        dataset_id=stable_id("historical_economic_series", *values.values()),
        collected_at=collected_at,
        **values,
    )
    return HistoricalEconomicSeriesDataset(manifest=manifest, observations=observations)


def _validate_dataset(dataset: HistoricalEconomicSeriesDataset) -> None:
    observations = dataset.observations
    manifest = dataset.manifest
    if len(observations) != manifest.observation_count:
        raise ValueError("经济代理观测数量与 Manifest 不一致")
    dates = tuple(item.effective_date for item in observations)
    if dates != tuple(sorted(set(dates))):
        raise ValueError("经济代理观测日期必须唯一且递增")
    if dates[0] != manifest.first_effective_date or dates[-1] != manifest.last_effective_date:
        raise ValueError("经济代理观测范围与 Manifest 不一致")
    if _observations_hash(observations) != manifest.observations_hash:
        raise ValueError("经济代理观测哈希与 Manifest 不一致")


def _observations_hash(observations: tuple[EconomicSeriesObservation, ...]) -> str:
    return content_hash(
        [[item.effective_date.isoformat(), str(item.value)] for item in observations]
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
