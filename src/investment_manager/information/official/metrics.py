from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from html.parser import HTMLParser
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
ISHARES_SOURCE_ID = "ishares"
ARK_SOURCE_ID = "ark-invest"
BITWISE_SOURCE_ID = "bitwise"

TGA_STREAM_ID = "treasury-tga-balance"
TREASURY_YIELD_STREAM_ID = "treasury-yield-curve"
FED_BROAD_DOLLAR_STREAM_ID = "fed-broad-dollar"
NYFED_RRP_STREAM_ID = "nyfed-reverse-repo"
NYFED_SOMA_STREAM_ID = "nyfed-soma-holdings"
NYFED_RATES_STREAM_ID = "nyfed-reference-rates"
IBIT_HOLDINGS_STREAM_ID = "ishares-ibit-holdings"
ARKB_HOLDINGS_STREAM_ID = "ark-arkb-holdings"
BITB_HOLDINGS_STREAM_ID = "bitwise-bitb-holdings"

TGA_FACT_TYPE = "US_TREASURY_CASH_SNAPSHOT"
TREASURY_YIELD_FACT_TYPE = "US_TREASURY_YIELD_CURVE_SNAPSHOT"
FED_BROAD_DOLLAR_FACT_TYPE = "FED_BROAD_DOLLAR_SNAPSHOT"
NYFED_RRP_FACT_TYPE = "NYFED_REVERSE_REPO_SNAPSHOT"
NYFED_SOMA_FACT_TYPE = "NYFED_SOMA_SNAPSHOT"
NYFED_RATES_FACT_TYPE = "NYFED_REFERENCE_RATES_SNAPSHOT"
IBIT_HOLDINGS_FACT_TYPE = "IBIT_HOLDINGS_SNAPSHOT"
ARKB_HOLDINGS_FACT_TYPE = "ARKB_HOLDINGS_SNAPSHOT"
BITB_HOLDINGS_FACT_TYPE = "BITB_HOLDINGS_SNAPSHOT"

OFFICIAL_METRIC_FACT_TYPES = frozenset(
    {
        TGA_FACT_TYPE,
        TREASURY_YIELD_FACT_TYPE,
        FED_BROAD_DOLLAR_FACT_TYPE,
        NYFED_RRP_FACT_TYPE,
        NYFED_SOMA_FACT_TYPE,
        NYFED_RATES_FACT_TYPE,
        IBIT_HOLDINGS_FACT_TYPE,
        ARKB_HOLDINGS_FACT_TYPE,
        BITB_HOLDINGS_FACT_TYPE,
    }
)
OFFICIAL_METRIC_RISK_FACTORS = frozenset(
    {
        "US_DOLLAR",
        "US_FISCAL_LIQUIDITY",
        "US_INTEREST_RATES",
        "US_MONETARY_LIQUIDITY",
        "US_MONETARY_POLICY",
        "BTC_INSTITUTIONAL_HOLDINGS",
    }
)


class OfficialMetricName(StrEnum):
    IBIT_BTC_HOLDINGS = "ibit_btc_holdings"
    IBIT_BTC_HOLDINGS_CHANGE_1D = "ibit_btc_holdings_change_1d"
    IBIT_NET_ASSETS_USD_M = "ibit_net_assets_usd_m"
    IBIT_SHARES_OUTSTANDING = "ibit_shares_outstanding"
    IBIT_SHARES_OUTSTANDING_CHANGE_1D = "ibit_shares_outstanding_change_1d"
    BTC_ETP_HOLDINGS = "btc_etp_holdings"
    BTC_ETP_HOLDINGS_CHANGE_1D = "btc_etp_holdings_change_1d"
    BTC_ETP_NET_ASSETS_USD_M = "btc_etp_net_assets_usd_m"
    BTC_ETP_SHARES_OUTSTANDING = "btc_etp_shares_outstanding"
    BTC_ETP_SHARES_OUTSTANDING_CHANGE_1D = (
        "btc_etp_shares_outstanding_change_1d"
    )
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
    BITCOIN = "BITCOIN"
    SHARES = "SHARES"
    USD_MILLIONS = "USD_MILLIONS"
    PERCENT = "PERCENT"
    BASIS_POINTS = "BASIS_POINTS"
    INDEX = "INDEX"


class OfficialMetricValue(FrozenModel):
    name: OfficialMetricName
    value: Decimal
    unit: OfficialMetricUnit


class OfficialMetricChangeContext(FrozenModel):
    """Point-in-time empirical scale for the snapshot's most unusual change.

    The percentile is calculated only from observations present in the same
    first-party response.  It lets downstream policy distinguish a routine
    update from a genuinely unusual move without encoding a bespoke absolute
    threshold for every series.
    """

    metric_name: OfficialMetricName
    latest_change: Decimal
    unit: OfficialMetricUnit
    absolute_change_percentile: Decimal = Field(ge=0, le=1)
    sample_size: int = Field(gt=0)
    lookback_start: date
    lookback_end: date

    @model_validator(mode="after")
    def lookback_must_be_ordered(self):
        if self.lookback_start > self.lookback_end:
            raise ValueError("官方指标变化背景的回看区间非法")
        return self


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
    change_context: OfficialMetricChangeContext | None = None
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
            "www.ishares.com",
            "assets.ark-funds.com",
            "bitbetf.com",
        }:
            raise ValueError("官方指标 URL 不在固定官方域名")
        names = tuple(item.name.value for item in self.metrics)
        if tuple(sorted(set(names))) != names:
            raise ValueError("官方指标必须按名称唯一且排序")
        if tuple(sorted(set(self.risk_factors))) != self.risk_factors:
            raise ValueError("官方指标 risk_factors 必须唯一且排序")
        if self.change_context is not None:
            contextual_metric = next(
                (
                    item
                    for item in self.metrics
                    if item.name == self.change_context.metric_name
                ),
                None,
            )
            if contextual_metric is None:
                raise ValueError("官方指标变化背景必须引用当前快照指标")
            if (
                contextual_metric.value != self.change_context.latest_change
                or contextual_metric.unit != self.change_context.unit
            ):
                raise ValueError("官方指标变化背景与当前指标值不一致")
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
    payload = {
        "stream_id": snapshot.stream_id,
        "domain": snapshot.domain.value,
        "fact_type": snapshot.fact_type,
        "effective_date": snapshot.effective_date.isoformat(),
        "headline": snapshot.headline,
        "risk_factors": snapshot.risk_factors,
        "metrics": snapshot.metrics,
        "source_url": snapshot.source_url,
    }
    if snapshot.change_context is not None:
        payload["change_context"] = snapshot.change_context
    return payload


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
        IBIT_HOLDINGS_STREAM_ID: _parse_ibit_holdings,
        ARKB_HOLDINGS_STREAM_ID: _parse_arkb_holdings,
        BITB_HOLDINGS_STREAM_ID: _parse_bitb_holdings,
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
    change_context = _most_unusual_change_context(
        ordered,
        candidates=(
            (
                OfficialMetricName.TGA_CHANGE_1D_USD_M,
                1,
                OfficialMetricUnit.USD_MILLIONS,
            ),
            (
                OfficialMetricName.TGA_CHANGE_5D_USD_M,
                5,
                OfficialMetricUnit.USD_MILLIONS,
            ),
        ),
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
        change_context=change_context,
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
    ten_year_history = tuple(
        (record_date, _decimal(values.get("BC_10YEAR"), name="Treasury 10Y"))
        for record_date, values in entries
    )
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
        change_context=_most_unusual_change_context(
            ten_year_history,
            candidates=(
                (
                    OfficialMetricName.TREASURY_10Y_CHANGE_1D_BPS,
                    1,
                    OfficialMetricUnit.BASIS_POINTS,
                    Decimal("100"),
                ),
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
        change_context=_most_unusual_change_context(
            observations,
            candidates=(
                (
                    OfficialMetricName.BROAD_DOLLAR_CHANGE_1D_PCT,
                    1,
                    OfficialMetricUnit.PERCENT,
                    Decimal("100"),
                    True,
                ),
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
        change_context=_most_unusual_change_context(
            observations,
            candidates=(
                (
                    OfficialMetricName.RRP_CHANGE_1D_USD_M,
                    1,
                    OfficialMetricUnit.USD_MILLIONS,
                ),
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
        change_context=_most_unusual_change_context(
            observations,
            candidates=(
                (
                    OfficialMetricName.SOMA_CHANGE_1W_USD_M,
                    1,
                    OfficialMetricUnit.USD_MILLIONS,
                ),
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


def _parse_ibit_holdings(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("IBIT holdings CSV 编码非法") from exc
    if len(lines) < 10 or lines[0].strip() != "iShares Bitcoin Trust ETF":
        raise ValueError("IBIT holdings CSV 缺少固定标题")
    metadata: dict[str, str] = {}
    header_index = None
    for index, line in enumerate(lines[1:], start=1):
        row = next(csv.reader((line,)))
        if row and row[0] == "Ticker":
            header_index = index
            break
        if len(row) >= 2:
            metadata[row[0]] = row[1]
    if header_index is None:
        raise ValueError("IBIT holdings CSV 缺少持仓表头")
    try:
        effective_date = datetime.strptime(
            metadata["Fund Holdings as of"], "%b %d, %Y"
        ).date()
        shares = _decimal(
            metadata["Shares Outstanding"].replace(",", ""),
            name="IBIT shares outstanding",
        )
    except KeyError as exc:
        raise ValueError("IBIT holdings CSV 缺少基金元数据") from exc
    holdings = tuple(csv.DictReader(lines[header_index:]))
    bitcoin = next((row for row in holdings if row.get("Ticker") == "BTC"), None)
    if bitcoin is None:
        raise ValueError("IBIT holdings CSV 缺少 BTC 持仓")
    btc_quantity = _decimal(
        str(bitcoin.get("Quantity", "")).replace(",", ""),
        name="IBIT BTC quantity",
    )
    market_value = _decimal(
        str(bitcoin.get("Market Value", "")).replace(",", ""),
        name="IBIT BTC market value",
    )
    return _snapshot(
        source_id=ISHARES_SOURCE_ID,
        stream_id=IBIT_HOLDINGS_STREAM_ID,
        domain=CausalDomain.INSTITUTIONAL_FLOWS,
        fact_type=IBIT_HOLDINGS_FACT_TYPE,
        effective_date=effective_date,
        headline="iShares Bitcoin Trust ETF daily holdings",
        risk_factors=("BTC_INSTITUTIONAL_HOLDINGS",),
        metrics=(
            _metric(
                OfficialMetricName.IBIT_BTC_HOLDINGS,
                btc_quantity,
                OfficialMetricUnit.BITCOIN,
            ),
            _metric(
                OfficialMetricName.IBIT_NET_ASSETS_USD_M,
                market_value / Decimal("1000000"),
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.IBIT_SHARES_OUTSTANDING,
                shares,
                OfficialMetricUnit.SHARES,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(effective_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_arkb_holdings(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    try:
        rows = tuple(csv.DictReader(content.decode("utf-8-sig").splitlines()))
    except UnicodeDecodeError as exc:
        raise ValueError("ARKB holdings CSV 编码非法") from exc
    bitcoin_rows = tuple(
        row
        for row in rows
        if row.get("fund") == "ARKB"
        and row.get("ticker") == "BTC"
        and row.get("company") == "BITCOIN"
    )
    if len(bitcoin_rows) != 1:
        raise ValueError("ARKB holdings CSV 缺少唯一 BTC 持仓")
    bitcoin = bitcoin_rows[0]
    try:
        effective_date = datetime.strptime(str(bitcoin["date"]), "%m/%d/%Y").date()
        btc_quantity = _decimal(
            str(bitcoin["shares"]).replace(",", ""),
            name="ARKB BTC quantity",
        )
        market_value = _decimal(
            str(bitcoin["market value ($)"]).replace("$", "").replace(",", ""),
            name="ARKB BTC market value",
        )
    except KeyError as exc:
        raise ValueError("ARKB holdings CSV 缺少固定字段") from exc
    return _snapshot(
        source_id=ARK_SOURCE_ID,
        stream_id=ARKB_HOLDINGS_STREAM_ID,
        domain=CausalDomain.INSTITUTIONAL_FLOWS,
        fact_type=ARKB_HOLDINGS_FACT_TYPE,
        effective_date=effective_date,
        headline="ARK 21Shares Bitcoin ETF daily holdings",
        risk_factors=("BTC_INSTITUTIONAL_HOLDINGS",),
        metrics=(
            _metric(
                OfficialMetricName.BTC_ETP_HOLDINGS,
                btc_quantity,
                OfficialMetricUnit.BITCOIN,
            ),
            _metric(
                OfficialMetricName.BTC_ETP_NET_ASSETS_USD_M,
                market_value / Decimal("1000000"),
                OfficialMetricUnit.USD_MILLIONS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(effective_date, observed_at),
        payload_ref=payload_ref,
    )


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside = False
        self.content: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        self._inside = tag == "script" and attributes.get("id") == "__NEXT_DATA__"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.content.append(data)


def _parse_bitb_holdings(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("BITB 页面编码非法") from exc
    parser = _NextDataParser()
    parser.feed(html)
    if not parser.content:
        raise ValueError("BITB 页面缺少 __NEXT_DATA__")
    document = _json_object("".join(parser.content).encode())
    try:
        page_props = document["props"]["pageProps"]
        data = page_props["fundData"]["data"]
        details = data["fundDetails"]
        holdings = data["holdings"]
        effective_date = _date(holdings["asOfDate"], name="BITB holdings asOfDate")
        if _date(details["asOfDate"], name="BITB details asOfDate") != effective_date:
            raise ValueError("BITB 持仓与基金详情日期不一致")
        bitcoin_rows = tuple(
            row
            for row in holdings["basket"]
            if isinstance(row, dict) and row.get("companyName") == "BITCOIN"
        )
        if len(bitcoin_rows) != 1:
            raise ValueError("BITB 页面缺少唯一 BTC 持仓")
        bitcoin = bitcoin_rows[0]
        btc_quantity = _decimal(bitcoin.get("shares"), name="BITB BTC quantity")
        market_value = _decimal(
            bitcoin.get("marketValue"),
            name="BITB BTC market value",
        )
        net_assets = _decimal(details.get("netAssets"), name="BITB net assets")
        shares_outstanding = _decimal(
            details.get("sharesOutstanding"),
            name="BITB shares outstanding",
        )
        source_published_at = min(
            _datetime(data.get("updatedAt"), name="BITB updatedAt"),
            observed_at,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("BITB 页面结构缺少固定字段") from exc
    if market_value <= 0 or net_assets <= 0 or shares_outstanding <= 0:
        raise ValueError("BITB 持仓、资产和份额必须为正数")
    return _snapshot(
        source_id=BITWISE_SOURCE_ID,
        stream_id=BITB_HOLDINGS_STREAM_ID,
        domain=CausalDomain.INSTITUTIONAL_FLOWS,
        fact_type=BITB_HOLDINGS_FACT_TYPE,
        effective_date=effective_date,
        headline="Bitwise Bitcoin ETF daily holdings",
        risk_factors=("BTC_INSTITUTIONAL_HOLDINGS",),
        metrics=(
            _metric(
                OfficialMetricName.BTC_ETP_HOLDINGS,
                btc_quantity,
                OfficialMetricUnit.BITCOIN,
            ),
            _metric(
                OfficialMetricName.BTC_ETP_NET_ASSETS_USD_M,
                net_assets / Decimal("1000000"),
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.BTC_ETP_SHARES_OUTSTANDING,
                shares_outstanding,
                OfficialMetricUnit.SHARES,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=source_published_at,
        payload_ref=payload_ref,
    )


def with_official_metric_history(
    snapshot: OfficialMetricSnapshot,
    history: tuple[OfficialMetricSnapshot, ...],
) -> OfficialMetricSnapshot:
    """Add revision-safe change context when a latest-only source has accumulated history."""

    policies = {
        IBIT_HOLDINGS_STREAM_ID: (
            OfficialMetricName.IBIT_BTC_HOLDINGS,
            OfficialMetricName.IBIT_BTC_HOLDINGS_CHANGE_1D,
            OfficialMetricName.IBIT_SHARES_OUTSTANDING,
            OfficialMetricName.IBIT_SHARES_OUTSTANDING_CHANGE_1D,
        ),
        ARKB_HOLDINGS_STREAM_ID: (
            OfficialMetricName.BTC_ETP_HOLDINGS,
            OfficialMetricName.BTC_ETP_HOLDINGS_CHANGE_1D,
            None,
            None,
        ),
        BITB_HOLDINGS_STREAM_ID: (
            OfficialMetricName.BTC_ETP_HOLDINGS,
            OfficialMetricName.BTC_ETP_HOLDINGS_CHANGE_1D,
            OfficialMetricName.BTC_ETP_SHARES_OUTSTANDING,
            OfficialMetricName.BTC_ETP_SHARES_OUTSTANDING_CHANGE_1D,
        ),
    }
    policy = policies.get(snapshot.stream_id)
    if policy is None:
        return snapshot
    holdings_name, holdings_change_name, shares_name, shares_change_name = policy
    by_date = {
        item.effective_date: item
        for item in history
        if item.stream_id == snapshot.stream_id
        and item.effective_date < snapshot.effective_date
    }
    ordered = tuple(by_date[key] for key in sorted(by_date))
    if not ordered:
        return snapshot
    holdings_history = tuple(
        (item.effective_date, _metric_value(item, holdings_name))
        for item in (*ordered, snapshot)
    )
    holdings_change = holdings_history[-1][1] - holdings_history[-2][1]
    additions = [
        _metric(
            holdings_change_name,
            holdings_change,
            OfficialMetricUnit.BITCOIN,
        )
    ]
    if shares_name is not None and shares_change_name is not None:
        shares_history = tuple(
            (item.effective_date, _metric_value(item, shares_name))
            for item in (*ordered, snapshot)
        )
        additions.append(
            _metric(
                shares_change_name,
                shares_history[-1][1] - shares_history[-2][1],
                OfficialMetricUnit.SHARES,
            )
        )
    metrics = (
        *snapshot.metrics,
        *additions,
    )
    context = _most_unusual_change_context(
        holdings_history,
        candidates=(
            (
                holdings_change_name,
                1,
                OfficialMetricUnit.BITCOIN,
            ),
        ),
    )
    return _snapshot(
        source_id=snapshot.observation.source_id,
        stream_id=snapshot.stream_id,
        domain=snapshot.domain,
        fact_type=snapshot.fact_type,
        effective_date=snapshot.effective_date,
        headline=snapshot.headline,
        risk_factors=snapshot.risk_factors,
        metrics=metrics,
        change_context=context,
        source_url=snapshot.source_url,
        observed_at=snapshot.observation.observed_at,
        source_published_at=snapshot.observation.source_published_at
        or snapshot.observation.observed_at,
        payload_ref=snapshot.observation.payload_ref,
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
    change_context: OfficialMetricChangeContext | None = None,
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
        change_context=change_context,
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
        OfficialMetricUnit.BITCOIN: Decimal("0.00000001"),
        OfficialMetricUnit.SHARES: Decimal("0.01"),
        OfficialMetricUnit.USD_MILLIONS: Decimal("0.001"),
        OfficialMetricUnit.PERCENT: Decimal("0.0001"),
        OfficialMetricUnit.BASIS_POINTS: Decimal("0.01"),
        OfficialMetricUnit.INDEX: Decimal("0.0001"),
    }[unit]
    rounded = value.quantize(precision, rounding=ROUND_HALF_EVEN)
    plain = format(rounded, "f").rstrip("0").rstrip(".")
    compact = Decimal(plain or "0")
    return OfficialMetricValue(name=name, value=compact, unit=unit)


def _metric_value(
    snapshot: OfficialMetricSnapshot,
    name: OfficialMetricName,
) -> Decimal:
    try:
        return next(item.value for item in snapshot.metrics if item.name == name)
    except StopIteration as exc:
        raise ValueError(f"官方指标快照缺少 {name.value}") from exc


def _most_unusual_change_context(
    observations: list[tuple[date, Decimal]] | tuple[tuple[date, Decimal], ...],
    *,
    candidates: tuple[
        tuple[OfficialMetricName, int, OfficialMetricUnit]
        | tuple[OfficialMetricName, int, OfficialMetricUnit, Decimal]
        | tuple[OfficialMetricName, int, OfficialMetricUnit, Decimal, bool],
        ...,
    ],
) -> OfficialMetricChangeContext | None:
    """Rank current absolute changes against the response's own history.

    Candidate tuples may provide a multiplier and request percentage-return
    changes.  Selecting the highest percentile keeps the projection compact
    while retaining the horizon on which the current move is most exceptional.
    """

    contexts: list[OfficialMetricChangeContext] = []
    ordered = tuple(sorted(observations, key=lambda item: item[0]))
    for candidate in candidates:
        name, lag, unit, *options = candidate
        multiplier = options[0] if options else Decimal("1")
        percentage_return = bool(options[1]) if len(options) > 1 else False
        if len(ordered) <= lag:
            continue
        changes: list[Decimal] = []
        for index in range(lag, len(ordered)):
            current = ordered[index][1]
            prior = ordered[index - lag][1]
            if percentage_return:
                if prior == 0:
                    continue
                change = (current / prior - 1) * multiplier
            else:
                change = (current - prior) * multiplier
            changes.append(change)
        if not changes:
            continue
        latest = changes[-1]
        percentile = Decimal(
            sum(abs(value) <= abs(latest) for value in changes)
        ) / Decimal(len(changes))
        rounded_latest = _metric(name, latest, unit).value
        contexts.append(
            OfficialMetricChangeContext(
                metric_name=name,
                latest_change=rounded_latest,
                unit=unit,
                absolute_change_percentile=percentile.quantize(Decimal("0.0001")),
                sample_size=len(changes),
                lookback_start=ordered[lag][0],
                lookback_end=ordered[-1][0],
            )
        )
    if not contexts:
        return None
    return max(
        contexts,
        key=lambda item: (
            item.absolute_change_percentile,
            item.sample_size,
            item.metric_name.value,
        ),
    )


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
    if stream_id == IBIT_HOLDINGS_STREAM_ID:
        return ISHARES_SOURCE_ID
    if stream_id == ARKB_HOLDINGS_STREAM_ID:
        return ARK_SOURCE_ID
    if stream_id == BITB_HOLDINGS_STREAM_ID:
        return BITWISE_SOURCE_ID
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
