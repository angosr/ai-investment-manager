from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse
from xml.etree import ElementTree

from pydantic import Field, model_validator

from investment_manager.information.models import (
    CausalDomain,
    SourceObservation,
    SourceTier,
)
from investment_manager.information.official.records import OfficialRecordKind
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

TREASURY_FISCAL_SOURCE_ID = "us-treasury-fiscal-data"
TREASURY_RATES_SOURCE_ID = "us-treasury-rates"
FED_H10_SOURCE_ID = "federal-reserve-h10"
NYFED_MARKETS_SOURCE_ID = "new-york-fed-markets"

TGA_STREAM_ID = "treasury-tga-balance"
TREASURY_YIELD_STREAM_ID = "treasury-yield-curve"
FED_BROAD_DOLLAR_STREAM_ID = "fed-broad-dollar"
NYFED_RRP_STREAM_ID = "nyfed-reverse-repo"
NYFED_SOMA_STREAM_ID = "nyfed-soma-holdings"
NYFED_RATES_STREAM_ID = "nyfed-reference-rates"

TGA_FACT_TYPE = "US_TREASURY_CASH_SNAPSHOT"
TREASURY_YIELD_FACT_TYPE = "US_TREASURY_YIELD_CURVE_SNAPSHOT"
FED_BROAD_DOLLAR_FACT_TYPE = "FED_BROAD_DOLLAR_SNAPSHOT"
NYFED_RRP_FACT_TYPE = "NYFED_REVERSE_REPO_SNAPSHOT"
NYFED_SOMA_FACT_TYPE = "NYFED_SOMA_SNAPSHOT"
NYFED_RATES_FACT_TYPE = "NYFED_REFERENCE_RATES_SNAPSHOT"

OFFICIAL_METRIC_FACT_TYPES = frozenset(
    {
        TGA_FACT_TYPE,
        TREASURY_YIELD_FACT_TYPE,
        FED_BROAD_DOLLAR_FACT_TYPE,
        NYFED_RRP_FACT_TYPE,
        NYFED_SOMA_FACT_TYPE,
        NYFED_RATES_FACT_TYPE,
    }
)
OFFICIAL_METRIC_RISK_FACTORS = frozenset(
    {
        "US_DOLLAR",
        "US_FISCAL_LIQUIDITY",
        "US_INTEREST_RATES",
        "US_MONETARY_LIQUIDITY",
        "US_MONETARY_POLICY",
    }
)


class OfficialMetricName(StrEnum):
    TGA_BALANCE_USD_M = "tga_balance_usd_m"
    TGA_CHANGE_1D_USD_M = "tga_change_1d_usd_m"
    TGA_CHANGE_5D_USD_M = "tga_change_5d_usd_m"
    TREASURY_2Y_PCT = "treasury_2y_pct"
    TREASURY_10Y_PCT = "treasury_10y_pct"
    TREASURY_30Y_PCT = "treasury_30y_pct"
    TREASURY_2S10S_BPS = "treasury_2s10s_bps"
    TREASURY_10Y_CHANGE_1D_BPS = "treasury_10y_change_1d_bps"
    TREASURY_30Y_CHANGE_1D_BPS = "treasury_30y_change_1d_bps"
    BROAD_DOLLAR_INDEX = "broad_dollar_index"
    BROAD_DOLLAR_CHANGE_1D_PCT = "broad_dollar_change_1d_pct"
    RRP_ACCEPTED_USD_M = "rrp_accepted_usd_m"
    RRP_CHANGE_1D_USD_M = "rrp_change_1d_usd_m"
    SOMA_TOTAL_USD_M = "soma_total_usd_m"
    SOMA_CHANGE_1W_USD_M = "soma_change_1w_usd_m"
    EFFR_PCT = "effr_pct"
    SOFR_PCT = "sofr_pct"
    FED_TARGET_LOWER_PCT = "fed_target_lower_pct"
    FED_TARGET_UPPER_PCT = "fed_target_upper_pct"
    SOFR_EFFR_SPREAD_BPS = "sofr_effr_spread_bps"


class OfficialMetricUnit(StrEnum):
    USD_MILLIONS = "USD_MILLIONS"
    PERCENT = "PERCENT"
    BASIS_POINTS = "BASIS_POINTS"
    INDEX = "INDEX"


class OfficialMetricValue(FrozenModel):
    name: OfficialMetricName
    value: Decimal
    unit: OfficialMetricUnit


class OfficialMetricSnapshot(FrozenModel):
    """One compact, revision-safe observation from a pinned first-party feed."""

    observation: SourceObservation
    kind: Literal[OfficialRecordKind.OFFICIAL_METRIC_SNAPSHOT] = (
        OfficialRecordKind.OFFICIAL_METRIC_SNAPSHOT
    )
    stream_id: str = Field(min_length=1, max_length=128)
    domain: CausalDomain
    fact_type: str = Field(min_length=1, max_length=80)
    effective_date: date
    headline: str = Field(min_length=1, max_length=200)
    risk_factors: tuple[str, ...] = Field(min_length=1)
    metrics: tuple[OfficialMetricValue, ...] = Field(min_length=1)
    source_url: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def identity_and_metrics_must_be_consistent(self):
        if self.observation.source_tier != SourceTier.FIRST_PARTY:
            raise ValueError("官方指标必须是一手来源")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "api.fiscaldata.treasury.gov",
            "home.treasury.gov",
            "www.federalreserve.gov",
            "markets.newyorkfed.org",
        }:
            raise ValueError("官方指标 URL 不在固定官方域名")
        names = tuple(item.name.value for item in self.metrics)
        if tuple(sorted(set(names))) != names:
            raise ValueError("官方指标必须按名称唯一且排序")
        if tuple(sorted(set(self.risk_factors))) != self.risk_factors:
            raise ValueError("官方指标 risk_factors 必须唯一且排序")
        expected_record_id = f"official-metric:{self.fact_type.lower()}"
        if self.observation.source_record_id != expected_record_id:
            raise ValueError("官方指标 source_record_id 与 fact_type 不一致")
        expected_hash = content_hash(metric_semantic_payload(self))
        if self.observation.payload_hash != expected_hash:
            raise ValueError("官方指标 payload_hash 与语义内容不一致")
        expected_observation_id = stable_id(
            "source_observation",
            self.observation.source_id,
            expected_record_id,
            expected_hash,
            self.observation.observed_at.isoformat(),
        )
        if self.observation.observation_id != expected_observation_id:
            raise ValueError("官方指标 observation_id 与内容不一致")
        return self


def metric_semantic_payload(snapshot: OfficialMetricSnapshot) -> dict:
    return {
        "stream_id": snapshot.stream_id,
        "domain": snapshot.domain.value,
        "fact_type": snapshot.fact_type,
        "effective_date": snapshot.effective_date.isoformat(),
        "headline": snapshot.headline,
        "risk_factors": snapshot.risk_factors,
        "metrics": snapshot.metrics,
        "source_url": snapshot.source_url,
    }


def parse_official_metric_document(
    stream_id: str,
    content: bytes,
    *,
    source_url: str,
    media_type: str,
    observed_at: datetime,
) -> OfficialMetricSnapshot:
    observed_at = require_utc(observed_at)
    parsers = {
        TGA_STREAM_ID: _parse_tga,
        TREASURY_YIELD_STREAM_ID: _parse_treasury_yields,
        FED_BROAD_DOLLAR_STREAM_ID: _parse_broad_dollar,
        NYFED_RRP_STREAM_ID: _parse_rrp,
        NYFED_SOMA_STREAM_ID: _parse_soma,
        NYFED_RATES_STREAM_ID: _parse_reference_rates,
    }
    parser = parsers.get(stream_id)
    if parser is None:
        raise ValueError(f"未知官方指标流: {stream_id}")
    raw = build_raw_source_payload(
        source_id=_stream_source_id(stream_id),
        source_url=source_url,
        media_type=media_type,
        observed_at=observed_at,
        content=content,
    )
    return parser(
        content,
        source_url=source_url,
        observed_at=observed_at,
        payload_ref=raw.payload_id,
    )


def _parse_tga(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    document = _json_object(content)
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ValueError("Treasury TGA API 缺少 data")
    balances: dict[date, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("account_type") != (
            "Treasury General Account (TGA) Closing Balance"
        ):
            continue
        record_date = _date(row.get("record_date"), name="TGA record_date")
        balances[record_date] = _decimal(row.get("open_today_bal"), name="TGA balance")
    ordered = sorted(balances.items())
    if len(ordered) < 2:
        raise ValueError("Treasury TGA API 缺少至少两个结算日")
    latest_date, latest = ordered[-1]
    previous = ordered[-2][1]
    metrics = [
        _metric(
            OfficialMetricName.TGA_BALANCE_USD_M,
            latest,
            OfficialMetricUnit.USD_MILLIONS,
        ),
        _metric(
            OfficialMetricName.TGA_CHANGE_1D_USD_M,
            latest - previous,
            OfficialMetricUnit.USD_MILLIONS,
        ),
    ]
    if len(ordered) >= 6:
        metrics.append(
            _metric(
                OfficialMetricName.TGA_CHANGE_5D_USD_M,
                latest - ordered[-6][1],
                OfficialMetricUnit.USD_MILLIONS,
            )
        )
    return _snapshot(
        source_id=TREASURY_FISCAL_SOURCE_ID,
        stream_id=TGA_STREAM_ID,
        domain=CausalDomain.FISCAL_DEBT,
        fact_type=TGA_FACT_TYPE,
        effective_date=latest_date,
        headline="U.S. Treasury General Account cash balance",
        risk_factors=("US_FISCAL_LIQUIDITY",),
        metrics=tuple(metrics),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(latest_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_treasury_yields(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    root = _xml(content, name="Treasury yield XML")
    atom = "{http://www.w3.org/2005/Atom}"
    metadata = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
    entries: list[tuple[date, dict[str, str]]] = []
    for entry in root.findall(f"{atom}entry"):
        properties = entry.find(f".//{metadata}properties")
        if properties is None:
            continue
        values = {item.tag.rsplit("}", 1)[-1]: (item.text or "").strip() for item in properties}
        entries.append(
            (_datetime(values.get("NEW_DATE"), name="Treasury yield date").date(), values)
        )
    entries.sort(key=lambda item: item[0])
    if len(entries) < 2:
        raise ValueError("Treasury yield XML 缺少至少两个交易日")
    latest_date, latest = entries[-1]
    previous = entries[-2][1]
    y2 = _decimal(latest.get("BC_2YEAR"), name="Treasury 2Y")
    y10 = _decimal(latest.get("BC_10YEAR"), name="Treasury 10Y")
    y30 = _decimal(latest.get("BC_30YEAR"), name="Treasury 30Y")
    previous_y10 = _decimal(previous.get("BC_10YEAR"), name="previous Treasury 10Y")
    previous_y30 = _decimal(previous.get("BC_30YEAR"), name="previous Treasury 30Y")
    updated = root.findtext(f"{atom}updated")
    published_at = _datetime(updated, name="Treasury yield updated")
    return _snapshot(
        source_id=TREASURY_RATES_SOURCE_ID,
        stream_id=TREASURY_YIELD_STREAM_ID,
        domain=CausalDomain.CROSS_ASSET_EXTERNAL,
        fact_type=TREASURY_YIELD_FACT_TYPE,
        effective_date=latest_date,
        headline="U.S. Treasury nominal yield curve",
        risk_factors=("US_INTEREST_RATES",),
        metrics=(
            _metric(OfficialMetricName.TREASURY_2Y_PCT, y2, OfficialMetricUnit.PERCENT),
            _metric(OfficialMetricName.TREASURY_10Y_PCT, y10, OfficialMetricUnit.PERCENT),
            _metric(OfficialMetricName.TREASURY_30Y_PCT, y30, OfficialMetricUnit.PERCENT),
            _metric(
                OfficialMetricName.TREASURY_2S10S_BPS,
                (y10 - y2) * 100,
                OfficialMetricUnit.BASIS_POINTS,
            ),
            _metric(
                OfficialMetricName.TREASURY_10Y_CHANGE_1D_BPS,
                (y10 - previous_y10) * 100,
                OfficialMetricUnit.BASIS_POINTS,
            ),
            _metric(
                OfficialMetricName.TREASURY_30Y_CHANGE_1D_BPS,
                (y30 - previous_y30) * 100,
                OfficialMetricUnit.BASIS_POINTS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=min(published_at, observed_at),
        payload_ref=payload_ref,
    )


def _parse_broad_dollar(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    root = _xml(content, name="Federal Reserve H10 XML")
    rss = "{http://purl.org/rss/1.0/}"
    dc = "{http://purl.org/dc/elements/1.1/}"
    cb = "{http://www.cbwiki.net/wiki/index.php/Specification_1.1}"
    observations: list[tuple[date, Decimal]] = []
    for item in root.findall(f"{rss}item"):
        period = item.findtext(f".//{cb}observationPeriod")
        value = item.findtext(f".//{cb}value")
        observations.append(
            (_date(period, name="H10 observation period"), _decimal(value, name="H10 value"))
        )
    observations.sort(key=lambda item: item[0])
    if len(observations) < 2:
        raise ValueError("Federal Reserve H10 XML 缺少至少两条观测")
    latest_date, latest = observations[-1]
    previous = observations[-2][1]
    channel_date = root.findtext(f"{rss}channel/{dc}date")
    return _snapshot(
        source_id=FED_H10_SOURCE_ID,
        stream_id=FED_BROAD_DOLLAR_STREAM_ID,
        domain=CausalDomain.CROSS_ASSET_EXTERNAL,
        fact_type=FED_BROAD_DOLLAR_FACT_TYPE,
        effective_date=latest_date,
        headline="Federal Reserve nominal broad dollar index",
        risk_factors=("US_DOLLAR",),
        metrics=(
            _metric(OfficialMetricName.BROAD_DOLLAR_INDEX, latest, OfficialMetricUnit.INDEX),
            _metric(
                OfficialMetricName.BROAD_DOLLAR_CHANGE_1D_PCT,
                (latest / previous - 1) * 100,
                OfficialMetricUnit.PERCENT,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=min(_datetime(channel_date, name="H10 channel date"), observed_at),
        payload_ref=payload_ref,
    )


def _parse_rrp(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    document = _json_object(content)
    rows = document.get("repo", {}).get("operations")
    if not isinstance(rows, list):
        raise ValueError("NY Fed reverse repo API 缺少 operations")
    observations = sorted(
        (
            _date(row.get("operationDate"), name="RRP operation date"),
            _decimal(row.get("totalAmtAccepted"), name="RRP accepted") / Decimal("1000000"),
        )
        for row in rows
        if isinstance(row, dict) and row.get("operationType") == "Reverse Repo"
    )
    if len(observations) < 2:
        raise ValueError("NY Fed reverse repo API 缺少至少两条观测")
    latest_date, latest = observations[-1]
    previous = observations[-2][1]
    return _snapshot(
        source_id=NYFED_MARKETS_SOURCE_ID,
        stream_id=NYFED_RRP_STREAM_ID,
        domain=CausalDomain.MONETARY_INFLATION,
        fact_type=NYFED_RRP_FACT_TYPE,
        effective_date=latest_date,
        headline="New York Fed overnight reverse repo operation",
        risk_factors=("US_MONETARY_LIQUIDITY",),
        metrics=(
            _metric(OfficialMetricName.RRP_ACCEPTED_USD_M, latest, OfficialMetricUnit.USD_MILLIONS),
            _metric(
                OfficialMetricName.RRP_CHANGE_1D_USD_M,
                latest - previous,
                OfficialMetricUnit.USD_MILLIONS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(latest_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_soma(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    document = _json_object(content)
    rows = document.get("soma", {}).get("summary")
    if not isinstance(rows, list):
        raise ValueError("NY Fed SOMA API 缺少 summary")
    observations = sorted(
        (
            _date(row.get("asOfDate"), name="SOMA as-of date"),
            _decimal(row.get("total"), name="SOMA total") / Decimal("1000000"),
        )
        for row in rows
        if isinstance(row, dict)
    )
    if len(observations) < 2:
        raise ValueError("NY Fed SOMA API 缺少至少两条观测")
    latest_date, latest = observations[-1]
    previous = observations[-2][1]
    return _snapshot(
        source_id=NYFED_MARKETS_SOURCE_ID,
        stream_id=NYFED_SOMA_STREAM_ID,
        domain=CausalDomain.MONETARY_INFLATION,
        fact_type=NYFED_SOMA_FACT_TYPE,
        effective_date=latest_date,
        headline="New York Fed System Open Market Account holdings",
        risk_factors=("US_MONETARY_LIQUIDITY",),
        metrics=(
            _metric(OfficialMetricName.SOMA_TOTAL_USD_M, latest, OfficialMetricUnit.USD_MILLIONS),
            _metric(
                OfficialMetricName.SOMA_CHANGE_1W_USD_M,
                latest - previous,
                OfficialMetricUnit.USD_MILLIONS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(latest_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_reference_rates(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    document = _json_object(content)
    rows = document.get("refRates")
    if not isinstance(rows, list):
        raise ValueError("NY Fed reference rates API 缺少 refRates")
    by_type = {str(row.get("type")): row for row in rows if isinstance(row, dict)}
    effr = by_type.get("EFFR")
    sofr = by_type.get("SOFR")
    if effr is None or sofr is None:
        raise ValueError("NY Fed reference rates API 缺少 EFFR 或 SOFR")
    effective_date = max(
        _date(effr.get("effectiveDate"), name="EFFR date"),
        _date(sofr.get("effectiveDate"), name="SOFR date"),
    )
    effr_rate = _decimal(effr.get("percentRate"), name="EFFR")
    sofr_rate = _decimal(sofr.get("percentRate"), name="SOFR")
    return _snapshot(
        source_id=NYFED_MARKETS_SOURCE_ID,
        stream_id=NYFED_RATES_STREAM_ID,
        domain=CausalDomain.MONETARY_INFLATION,
        fact_type=NYFED_RATES_FACT_TYPE,
        effective_date=effective_date,
        headline="New York Fed reference rates",
        risk_factors=("US_MONETARY_POLICY",),
        metrics=(
            _metric(OfficialMetricName.EFFR_PCT, effr_rate, OfficialMetricUnit.PERCENT),
            _metric(OfficialMetricName.SOFR_PCT, sofr_rate, OfficialMetricUnit.PERCENT),
            _metric(
                OfficialMetricName.FED_TARGET_LOWER_PCT,
                _decimal(effr.get("targetRateFrom"), name="Fed target lower"),
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.FED_TARGET_UPPER_PCT,
                _decimal(effr.get("targetRateTo"), name="Fed target upper"),
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.SOFR_EFFR_SPREAD_BPS,
                (sofr_rate - effr_rate) * 100,
                OfficialMetricUnit.BASIS_POINTS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(effective_date, observed_at),
        payload_ref=payload_ref,
    )


def _snapshot(
    *,
    source_id: str,
    stream_id: str,
    domain: CausalDomain,
    fact_type: str,
    effective_date: date,
    headline: str,
    risk_factors: tuple[str, ...],
    metrics: tuple[OfficialMetricValue, ...],
    source_url: str,
    observed_at: datetime,
    source_published_at: datetime,
    payload_ref: str,
) -> OfficialMetricSnapshot:
    ordered_metrics = tuple(sorted(metrics, key=lambda item: item.name.value))
    ordered_risks = tuple(sorted(risk_factors))
    draft = OfficialMetricSnapshot.model_construct(
        observation=SourceObservation.model_construct(
            observation_id="pending",
            source_id=source_id,
            source_tier=SourceTier.FIRST_PARTY,
            source_record_id=f"official-metric:{fact_type.lower()}",
            observed_at=observed_at,
            source_published_at=source_published_at,
            payload_hash="0" * 64,
            payload_ref=payload_ref,
        ),
        stream_id=stream_id,
        domain=domain,
        fact_type=fact_type,
        effective_date=effective_date,
        headline=headline,
        risk_factors=ordered_risks,
        metrics=ordered_metrics,
        source_url=source_url,
    )
    payload_hash = content_hash(metric_semantic_payload(draft))
    record_id = draft.observation.source_record_id
    observation = SourceObservation(
        **draft.observation.model_dump(exclude={"observation_id", "payload_hash"}),
        payload_hash=payload_hash,
        observation_id=stable_id(
            "source_observation",
            source_id,
            record_id,
            payload_hash,
            observed_at.isoformat(),
        ),
    )
    return OfficialMetricSnapshot(
        **draft.model_dump(exclude={"observation"}),
        observation=observation,
    )


def _metric(
    name: OfficialMetricName,
    value: Decimal,
    unit: OfficialMetricUnit,
) -> OfficialMetricValue:
    precision = {
        OfficialMetricUnit.USD_MILLIONS: Decimal("0.001"),
        OfficialMetricUnit.PERCENT: Decimal("0.0001"),
        OfficialMetricUnit.BASIS_POINTS: Decimal("0.01"),
        OfficialMetricUnit.INDEX: Decimal("0.0001"),
    }[unit]
    rounded = value.quantize(precision, rounding=ROUND_HALF_EVEN)
    plain = format(rounded, "f").rstrip("0").rstrip(".")
    compact = Decimal(plain or "0")
    return OfficialMetricValue(name=name, value=compact, unit=unit)


def _effective_at(value: date, observed_at: datetime) -> datetime:
    """Use the observation date as a conservative freshness timestamp when APIs omit one."""

    return min(datetime.combine(value, time.min, tzinfo=UTC), observed_at)


def _stream_source_id(stream_id: str) -> str:
    if stream_id == TGA_STREAM_ID:
        return TREASURY_FISCAL_SOURCE_ID
    if stream_id == TREASURY_YIELD_STREAM_ID:
        return TREASURY_RATES_SOURCE_ID
    if stream_id == FED_BROAD_DOLLAR_STREAM_ID:
        return FED_H10_SOURCE_ID
    if stream_id in {NYFED_RRP_STREAM_ID, NYFED_SOMA_STREAM_ID, NYFED_RATES_STREAM_ID}:
        return NYFED_MARKETS_SOURCE_ID
    raise ValueError(f"未知官方指标流: {stream_id}")


def _json_object(content: bytes) -> dict:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("官方指标 JSON 非法") from exc
    if not isinstance(value, dict):
        raise ValueError("官方指标 JSON 必须为对象")
    return value


def _xml(content: bytes, *, name: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{name} 非法") from exc


def _decimal(value: object, *, name: str) -> Decimal:
    if value in {None, "", "null", "ND"}:
        raise ValueError(f"{name} 缺失")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} 不是有效数值") from exc


def _date(value: object, *, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} 缺失")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValueError(f"{name} 非法") from exc


def _datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} 缺失")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} 非法") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
