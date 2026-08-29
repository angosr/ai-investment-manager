from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from html.parser import HTMLParser
from itertools import pairwise
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
TREASURY_DIRECT_SOURCE_ID = "us-treasury-direct"
TREASURY_RATES_SOURCE_ID = "us-treasury-rates"
FED_H10_SOURCE_ID = "federal-reserve-h10"
NYFED_MARKETS_SOURCE_ID = "new-york-fed-markets"
ISHARES_SOURCE_ID = "ishares"
ARK_SOURCE_ID = "ark-invest"
BITWISE_SOURCE_ID = "bitwise"
FRED_SOURCE_ID = "federal-reserve-bank-st-louis-fred"
DEFILLAMA_SOURCE_ID = "defillama"

TGA_STREAM_ID = "treasury-tga-balance"
TREASURY_AUCTION_STREAM_ID = "treasury-auction-results"
TREASURY_REFINANCING_STREAM_ID = "treasury-refinancing-profile"
TREASURY_AVERAGE_INTEREST_COST_STREAM_ID = "treasury-average-interest-cost"
TREASURY_INTEREST_EXPENSE_STREAM_ID = "treasury-interest-expense"
TREASURY_REAL_YIELD_STREAM_ID = "treasury-real-yield-curve"
TREASURY_YIELD_STREAM_ID = "treasury-yield-curve"
FED_BROAD_DOLLAR_STREAM_ID = "fed-broad-dollar"
NYFED_RRP_STREAM_ID = "nyfed-reverse-repo"
NYFED_SOMA_STREAM_ID = "nyfed-soma-holdings"
NYFED_RATES_STREAM_ID = "nyfed-reference-rates"
IBIT_HOLDINGS_STREAM_ID = "ishares-ibit-holdings"
ARKB_HOLDINGS_STREAM_ID = "ark-arkb-holdings"
BITB_HOLDINGS_STREAM_ID = "bitwise-bitb-holdings"
FRED_SP500_STREAM_ID = "fred-sp500"
FRED_HIGH_YIELD_OAS_STREAM_ID = "fred-us-high-yield-oas"
FRED_WTI_STREAM_ID = "fred-wti"
STABLECOIN_SUPPLY_STREAM_ID = "defillama-usd-stablecoin-supply"

TGA_FACT_TYPE = "US_TREASURY_CASH_SNAPSHOT"
TREASURY_AUCTION_FACT_TYPE = "US_TREASURY_AUCTION_ABSORPTION_SNAPSHOT"
TREASURY_REFINANCING_FACT_TYPE = "US_TREASURY_REFINANCING_PROFILE_SNAPSHOT"
TREASURY_AVERAGE_INTEREST_COST_FACT_TYPE = "US_TREASURY_AVERAGE_INTEREST_COST_SNAPSHOT"
TREASURY_INTEREST_EXPENSE_FACT_TYPE = "US_TREASURY_INTEREST_EXPENSE_SNAPSHOT"
TREASURY_REAL_YIELD_FACT_TYPE = "US_TREASURY_REAL_YIELD_CURVE_SNAPSHOT"
TREASURY_YIELD_FACT_TYPE = "US_TREASURY_YIELD_CURVE_SNAPSHOT"
FED_BROAD_DOLLAR_FACT_TYPE = "FED_BROAD_DOLLAR_SNAPSHOT"
NYFED_RRP_FACT_TYPE = "NYFED_REVERSE_REPO_SNAPSHOT"
NYFED_SOMA_FACT_TYPE = "NYFED_SOMA_SNAPSHOT"
NYFED_RATES_FACT_TYPE = "NYFED_REFERENCE_RATES_SNAPSHOT"
IBIT_HOLDINGS_FACT_TYPE = "IBIT_HOLDINGS_SNAPSHOT"
ARKB_HOLDINGS_FACT_TYPE = "ARKB_HOLDINGS_SNAPSHOT"
BITB_HOLDINGS_FACT_TYPE = "BITB_HOLDINGS_SNAPSHOT"
US_EQUITY_MARKET_FACT_TYPE = "US_EQUITY_MARKET_SNAPSHOT"
US_HIGH_YIELD_CREDIT_FACT_TYPE = "US_HIGH_YIELD_CREDIT_SNAPSHOT"
US_WTI_OIL_FACT_TYPE = "US_WTI_OIL_SNAPSHOT"
USD_STABLECOIN_SUPPLY_FACT_TYPE = "USD_STABLECOIN_SUPPLY_SNAPSHOT"

OFFICIAL_METRIC_RISK_FACTORS_BY_TYPE = {
    TGA_FACT_TYPE: frozenset({"US_FISCAL_LIQUIDITY"}),
    TREASURY_AUCTION_FACT_TYPE: frozenset({"US_FISCAL_LIQUIDITY", "US_INTEREST_RATES"}),
    TREASURY_REFINANCING_FACT_TYPE: frozenset({"US_FISCAL_CAPACITY"}),
    TREASURY_AVERAGE_INTEREST_COST_FACT_TYPE: frozenset({"US_FISCAL_CAPACITY"}),
    TREASURY_INTEREST_EXPENSE_FACT_TYPE: frozenset({"US_FISCAL_CAPACITY"}),
    TREASURY_REAL_YIELD_FACT_TYPE: frozenset({"US_REAL_INTEREST_RATES"}),
    TREASURY_YIELD_FACT_TYPE: frozenset({"US_INTEREST_RATES"}),
    FED_BROAD_DOLLAR_FACT_TYPE: frozenset({"US_DOLLAR"}),
    NYFED_RRP_FACT_TYPE: frozenset({"US_MONETARY_LIQUIDITY"}),
    NYFED_SOMA_FACT_TYPE: frozenset({"US_MONETARY_LIQUIDITY"}),
    NYFED_RATES_FACT_TYPE: frozenset({"US_MONETARY_POLICY"}),
    IBIT_HOLDINGS_FACT_TYPE: frozenset({"BTC_INSTITUTIONAL_HOLDINGS"}),
    ARKB_HOLDINGS_FACT_TYPE: frozenset({"BTC_INSTITUTIONAL_HOLDINGS"}),
    BITB_HOLDINGS_FACT_TYPE: frozenset({"BTC_INSTITUTIONAL_HOLDINGS"}),
    US_EQUITY_MARKET_FACT_TYPE: frozenset({"US_EQUITY_RISK_APPETITE"}),
    US_HIGH_YIELD_CREDIT_FACT_TYPE: frozenset({"US_HIGH_YIELD_CREDIT_RISK"}),
    US_WTI_OIL_FACT_TYPE: frozenset({"US_ENERGY_INFLATION"}),
    USD_STABLECOIN_SUPPLY_FACT_TYPE: frozenset({"CRYPTO_LIQUIDITY_CAPACITY"}),
}
OFFICIAL_METRIC_FACT_TYPES = frozenset(OFFICIAL_METRIC_RISK_FACTORS_BY_TYPE)
OFFICIAL_METRIC_FACT_TYPES_BY_POLICY_VERSION = {
    "official-fact-v20": OFFICIAL_METRIC_FACT_TYPES
    - {
        TREASURY_REFINANCING_FACT_TYPE,
        TREASURY_AVERAGE_INTEREST_COST_FACT_TYPE,
        TREASURY_INTEREST_EXPENSE_FACT_TYPE,
    },
    "official-fact-v21": OFFICIAL_METRIC_FACT_TYPES,
}


class OfficialMetricName(StrEnum):
    IBIT_BTC_HOLDINGS = "ibit_btc_holdings"
    IBIT_BTC_HOLDINGS_CHANGE_1D = "ibit_btc_holdings_change_1d"
    IBIT_HOLDINGS_MARKET_VALUE_USD_M = "ibit_holdings_market_value_usd_m"
    # Immutable observations emitted before official-fact-v10 remain readable;
    # new parsers never emit this mislabelled historical name.
    IBIT_NET_ASSETS_USD_M = "ibit_net_assets_usd_m"
    IBIT_SHARES_OUTSTANDING = "ibit_shares_outstanding"
    IBIT_SHARES_OUTSTANDING_CHANGE_1D = "ibit_shares_outstanding_change_1d"
    BTC_ETP_HOLDINGS = "btc_etp_holdings"
    BTC_ETP_HOLDINGS_CHANGE_1D = "btc_etp_holdings_change_1d"
    BTC_ETP_HOLDINGS_MARKET_VALUE_USD_M = "btc_etp_holdings_market_value_usd_m"
    BTC_ETP_NET_ASSETS_USD_M = "btc_etp_net_assets_usd_m"
    BTC_ETP_SHARES_OUTSTANDING = "btc_etp_shares_outstanding"
    BTC_ETP_SHARES_OUTSTANDING_CHANGE_1D = "btc_etp_shares_outstanding_change_1d"
    TGA_BALANCE_USD_M = "tga_balance_usd_m"
    TGA_CHANGE_1D_USD_M = "tga_change_1d_usd_m"
    TGA_CHANGE_5D_USD_M = "tga_change_5d_usd_m"
    TREASURY_BILL_OFFERING_14D_USD_M = "treasury_bill_offering_14d_usd_m"
    TREASURY_COUPON_OFFERING_14D_USD_M = "treasury_coupon_offering_14d_usd_m"
    TREASURY_COUPON_BID_TO_COVER = "treasury_coupon_bid_to_cover"
    TREASURY_COUPON_DIRECT_SHARE_PCT = "treasury_coupon_direct_share_pct"
    TREASURY_COUPON_INDIRECT_SHARE_PCT = "treasury_coupon_indirect_share_pct"
    TREASURY_COUPON_PRIMARY_DEALER_SHARE_PCT = "treasury_coupon_primary_dealer_share_pct"
    TREASURY_COUPON_SOMA_ADDON_14D_USD_M = "treasury_coupon_soma_addon_14d_usd_m"
    TREASURY_MARKETABLE_DEBT_OUTSTANDING_USD_M = "treasury_marketable_debt_outstanding_usd_m"
    TREASURY_DEBT_MATURING_1Y_USD_M = "treasury_debt_maturing_1y_usd_m"
    TREASURY_DEBT_MATURING_1Y_PCT = "treasury_debt_maturing_1y_pct"
    TREASURY_DEBT_MATURING_3Y_PCT = "treasury_debt_maturing_3y_pct"
    TREASURY_WEIGHTED_REMAINING_MATURITY_YEARS = "treasury_weighted_remaining_maturity_years"
    TREASURY_MARKETABLE_AVG_INTEREST_RATE_PCT = "treasury_marketable_avg_interest_rate_pct"
    TREASURY_MARKETABLE_AVG_INTEREST_RATE_CHANGE_1Y_BPS = (
        "treasury_marketable_avg_interest_rate_change_1y_bps"
    )
    TREASURY_TOTAL_AVG_INTEREST_RATE_PCT = "treasury_total_avg_interest_rate_pct"
    TREASURY_TOTAL_AVG_INTEREST_RATE_CHANGE_1Y_BPS = (
        "treasury_total_avg_interest_rate_change_1y_bps"
    )
    TREASURY_INTEREST_EXPENSE_MONTH_USD_M = "treasury_interest_expense_month_usd_m"
    TREASURY_INTEREST_EXPENSE_MONTH_CHANGE_1Y_PCT = "treasury_interest_expense_month_change_1y_pct"
    TREASURY_INTEREST_EXPENSE_FYTD_USD_M = "treasury_interest_expense_fytd_usd_m"
    TREASURY_INTEREST_EXPENSE_FYTD_CHANGE_1Y_PCT = "treasury_interest_expense_fytd_change_1y_pct"
    TREASURY_2Y_PCT = "treasury_2y_pct"
    TREASURY_2Y_CHANGE_1D_BPS = "treasury_2y_change_1d_bps"
    TREASURY_10Y_PCT = "treasury_10y_pct"
    TREASURY_30Y_PCT = "treasury_30y_pct"
    TREASURY_2S10S_BPS = "treasury_2s10s_bps"
    TREASURY_10Y_CHANGE_1D_BPS = "treasury_10y_change_1d_bps"
    TREASURY_30Y_CHANGE_1D_BPS = "treasury_30y_change_1d_bps"
    TREASURY_REAL_5Y_PCT = "treasury_real_5y_pct"
    TREASURY_REAL_10Y_PCT = "treasury_real_10y_pct"
    TREASURY_REAL_30Y_PCT = "treasury_real_30y_pct"
    TREASURY_REAL_10Y_CHANGE_1D_BPS = "treasury_real_10y_change_1d_bps"
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
    SP500_INDEX = "sp500_index"
    SP500_CHANGE_1D_PCT = "sp500_change_1d_pct"
    US_HIGH_YIELD_OAS_PCT = "us_high_yield_oas_pct"
    US_HIGH_YIELD_OAS_CHANGE_1D_BPS = "us_high_yield_oas_change_1d_bps"
    WTI_USD_PER_BARREL = "wti_usd_per_barrel"
    WTI_CHANGE_1D_PCT = "wti_change_1d_pct"
    USD_STABLECOIN_SUPPLY_USD_M = "usd_stablecoin_supply_usd_m"
    USD_STABLECOIN_SUPPLY_CHANGE_1D_USD_M = "usd_stablecoin_supply_change_1d_usd_m"
    USD_STABLECOIN_SUPPLY_CHANGE_7D_USD_M = "usd_stablecoin_supply_change_7d_usd_m"
    USD_STABLECOIN_SUPPLY_CHANGE_30D_USD_M = "usd_stablecoin_supply_change_30d_usd_m"


class OfficialMetricUnit(StrEnum):
    BITCOIN = "BITCOIN"
    SHARES = "SHARES"
    USD_MILLIONS = "USD_MILLIONS"
    PERCENT = "PERCENT"
    BASIS_POINTS = "BASIS_POINTS"
    INDEX = "INDEX"
    USD_PER_BARREL = "USD_PER_BARREL"
    YEARS = "YEARS"


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
    """One compact, revision-safe observation from a pinned metric feed.

    The historical class name is retained for durable-record compatibility.
    Evidence tier remains explicit: a trusted aggregator must never be presented
    as the first-party producer of the underlying series.
    """

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
        if self.observation.source_tier not in {
            SourceTier.FIRST_PARTY,
            SourceTier.AGGREGATOR,
        }:
            raise ValueError("结构化指标必须是一手来源或明确标注的聚合来源")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "api.fiscaldata.treasury.gov",
            "home.treasury.gov",
            "www.treasurydirect.gov",
            "www.federalreserve.gov",
            "markets.newyorkfed.org",
            "www.ishares.com",
            "assets.ark-funds.com",
            "bitbetf.com",
            "fred.stlouisfed.org",
            "stablecoins.llama.fi",
        }:
            raise ValueError("结构化指标 URL 不在固定可信域名")
        names = tuple(item.name.value for item in self.metrics)
        if tuple(sorted(set(names))) != names:
            raise ValueError("官方指标必须按名称唯一且排序")
        if tuple(sorted(set(self.risk_factors))) != self.risk_factors:
            raise ValueError("官方指标 risk_factors 必须唯一且排序")
        if self.change_context is not None:
            contextual_metric = next(
                (item for item in self.metrics if item.name == self.change_context.metric_name),
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
        TREASURY_AUCTION_STREAM_ID: _parse_treasury_auctions,
        TREASURY_REFINANCING_STREAM_ID: _parse_treasury_refinancing,
        TREASURY_AVERAGE_INTEREST_COST_STREAM_ID: _parse_treasury_average_interest_cost,
        TREASURY_INTEREST_EXPENSE_STREAM_ID: _parse_treasury_interest_expense,
        TREASURY_REAL_YIELD_STREAM_ID: _parse_treasury_real_yields,
        TREASURY_YIELD_STREAM_ID: _parse_treasury_yields,
        FED_BROAD_DOLLAR_STREAM_ID: _parse_broad_dollar,
        NYFED_RRP_STREAM_ID: _parse_rrp,
        NYFED_SOMA_STREAM_ID: _parse_soma,
        NYFED_RATES_STREAM_ID: _parse_reference_rates,
        IBIT_HOLDINGS_STREAM_ID: _parse_ibit_holdings,
        ARKB_HOLDINGS_STREAM_ID: _parse_arkb_holdings,
        BITB_HOLDINGS_STREAM_ID: _parse_bitb_holdings,
        FRED_SP500_STREAM_ID: _parse_fred_sp500,
        FRED_HIGH_YIELD_OAS_STREAM_ID: _parse_fred_high_yield_oas,
        FRED_WTI_STREAM_ID: _parse_fred_wti,
        STABLECOIN_SUPPLY_STREAM_ID: _parse_stablecoin_supply,
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


def _parse_treasury_auctions(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    """Compress official auction results into current financing absorption.

    TreasuryDirect publishes announcement rows before results are available.  Only
    rows with accepted amounts are observations; announced future supply is not
    silently treated as completed financing.  The state separates bills from
    coupons and preserves who absorbed coupon duration without assigning an asset
    direction or a hidden policy motive.
    """

    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("TreasuryDirect auction JSON 非法") from exc
    if not isinstance(document, list):
        raise ValueError("TreasuryDirect auction JSON 必须为数组")
    results: list[dict[str, object]] = []
    for row in document:
        if not isinstance(row, dict) or row.get("totalAccepted") in {None, ""}:
            continue
        auction_date = _date(row.get("auctionDate"), name="auction date")
        if auction_date > observed_at.date():
            continue
        results.append({**row, "_auction_date": auction_date})
    if not results:
        raise ValueError("TreasuryDirect auction JSON 不含已公布结果")

    latest_date = max(row["_auction_date"] for row in results)
    window_start = latest_date - timedelta(days=13)
    current = [row for row in results if row["_auction_date"] >= window_start]
    bills = [row for row in current if row.get("type") == "Bill"]
    coupons = [row for row in current if row.get("type") != "Bill"]

    def total(rows: list[dict[str, object]], field: str) -> Decimal:
        return sum((_decimal(row.get(field), name=field) for row in rows), Decimal("0"))

    metrics = [
        _metric(
            OfficialMetricName.TREASURY_BILL_OFFERING_14D_USD_M,
            total(bills, "offeringAmount") / Decimal("1000000"),
            OfficialMetricUnit.USD_MILLIONS,
        ),
        _metric(
            OfficialMetricName.TREASURY_COUPON_OFFERING_14D_USD_M,
            total(coupons, "offeringAmount") / Decimal("1000000"),
            OfficialMetricUnit.USD_MILLIONS,
        ),
    ]
    if coupons:
        offered = total(coupons, "offeringAmount")
        accepted = total(coupons, "totalAccepted")
        soma = total(coupons, "somaAccepted")
        private_accepted = accepted - soma
        if offered <= 0 or private_accepted <= 0:
            raise ValueError("TreasuryDirect coupon auction 金额非法")
        bid_to_cover = (
            sum(
                _decimal(row.get("bidToCoverRatio"), name="bidToCoverRatio")
                * _decimal(row.get("offeringAmount"), name="offeringAmount")
                for row in coupons
            )
            / offered
        )
        metrics.extend(
            (
                _metric(
                    OfficialMetricName.TREASURY_COUPON_BID_TO_COVER,
                    bid_to_cover,
                    OfficialMetricUnit.INDEX,
                ),
                _metric(
                    OfficialMetricName.TREASURY_COUPON_DIRECT_SHARE_PCT,
                    total(coupons, "directBidderAccepted") / private_accepted * Decimal("100"),
                    OfficialMetricUnit.PERCENT,
                ),
                _metric(
                    OfficialMetricName.TREASURY_COUPON_INDIRECT_SHARE_PCT,
                    total(coupons, "indirectBidderAccepted") / private_accepted * Decimal("100"),
                    OfficialMetricUnit.PERCENT,
                ),
                _metric(
                    OfficialMetricName.TREASURY_COUPON_PRIMARY_DEALER_SHARE_PCT,
                    total(coupons, "primaryDealerAccepted") / private_accepted * Decimal("100"),
                    OfficialMetricUnit.PERCENT,
                ),
                _metric(
                    OfficialMetricName.TREASURY_COUPON_SOMA_ADDON_14D_USD_M,
                    soma / Decimal("1000000"),
                    OfficialMetricUnit.USD_MILLIONS,
                ),
            )
        )
    return _snapshot(
        source_id=TREASURY_DIRECT_SOURCE_ID,
        stream_id=TREASURY_AUCTION_STREAM_ID,
        domain=CausalDomain.FISCAL_DEBT,
        fact_type=TREASURY_AUCTION_FACT_TYPE,
        effective_date=latest_date,
        headline="U.S. Treasury auction absorption over the latest 14 days",
        risk_factors=("US_FISCAL_LIQUIDITY", "US_INTEREST_RATES"),
        metrics=tuple(metrics),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(latest_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_treasury_refinancing(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    """Compress the current marketable debt stock and maturity wall.

    FiscalData exposes reopened securities as additional rows while only one row
    carries the outstanding amount.  Requiring a numeric amount and maturity
    therefore avoids double counting without relying on a security-name list.
    """

    document = _json_object(content)
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ValueError("Treasury MSPD API 缺少 data")
    eligible_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and _date(row.get("record_date"), name="MSPD record_date") <= observed_at.date()
    ]
    if not eligible_rows:
        raise ValueError("Treasury MSPD API 不含截止时点前记录")
    effective_date = max(
        _date(row.get("record_date"), name="MSPD record_date") for row in eligible_rows
    )
    current = [
        row
        for row in eligible_rows
        if _date(row.get("record_date"), name="MSPD record_date") == effective_date
    ]
    total_rows = [row for row in current if row.get("security_class1_desc") == "Total Marketable"]
    if len(total_rows) != 1:
        raise ValueError("Treasury MSPD API 缺少唯一 Total Marketable")
    total_outstanding = _decimal(
        total_rows[0].get("outstanding_amt"), name="marketable debt outstanding"
    )
    if total_outstanding <= 0:
        raise ValueError("Treasury MSPD marketable debt outstanding 必须为正数")

    securities: list[tuple[Decimal, int]] = []
    for row in current:
        maturity_value = row.get("maturity_date")
        amount_value = row.get("outstanding_amt")
        if maturity_value in {None, "", "null"} or amount_value in {
            None,
            "",
            "null",
            "*",
        }:
            continue
        maturity = _date(maturity_value, name="MSPD maturity_date")
        remaining_days = (maturity - effective_date).days
        amount = _decimal(amount_value, name="MSPD outstanding_amt")
        if remaining_days >= 0 and amount > 0:
            securities.append((amount, remaining_days))
    if not securities:
        raise ValueError("Treasury MSPD API 不含可计算的未到期证券")
    represented_outstanding = sum((amount for amount, _ in securities), Decimal("0"))
    if represented_outstanding <= 0:
        raise ValueError("Treasury MSPD 未到期证券余额必须为正数")
    maturing_1y = sum((amount for amount, days in securities if days <= 365), Decimal("0"))
    maturing_3y = sum((amount for amount, days in securities if days <= 1095), Decimal("0"))
    weighted_maturity_years = (
        sum((amount * days for amount, days in securities), Decimal("0"))
        / represented_outstanding
        / Decimal("365.2425")
    )
    return _snapshot(
        source_id=TREASURY_FISCAL_SOURCE_ID,
        stream_id=TREASURY_REFINANCING_STREAM_ID,
        domain=CausalDomain.FISCAL_DEBT,
        fact_type=TREASURY_REFINANCING_FACT_TYPE,
        effective_date=effective_date,
        headline="U.S. Treasury marketable debt refinancing profile",
        risk_factors=("US_FISCAL_CAPACITY",),
        metrics=(
            _metric(
                OfficialMetricName.TREASURY_MARKETABLE_DEBT_OUTSTANDING_USD_M,
                total_outstanding,
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.TREASURY_DEBT_MATURING_1Y_USD_M,
                maturing_1y,
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.TREASURY_DEBT_MATURING_1Y_PCT,
                maturing_1y / total_outstanding * Decimal("100"),
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_DEBT_MATURING_3Y_PCT,
                maturing_3y / total_outstanding * Decimal("100"),
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_WEIGHTED_REMAINING_MATURITY_YEARS,
                weighted_maturity_years,
                OfficialMetricUnit.YEARS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(effective_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_treasury_average_interest_cost(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    document = _json_object(content)
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ValueError("Treasury average interest rate API 缺少 data")
    values: dict[date, dict[str, Decimal]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        record_date = _date(row.get("record_date"), name="average rate record_date")
        if record_date > observed_at.date():
            continue
        description = row.get("security_desc")
        if description not in {"Total Marketable", "Total Interest-bearing Debt"}:
            continue
        values.setdefault(record_date, {})[str(description)] = _decimal(
            row.get("avg_interest_rate_amt"), name="average interest rate"
        )
    complete_dates = sorted(
        record_date
        for record_date, by_description in values.items()
        if {"Total Marketable", "Total Interest-bearing Debt"} <= set(by_description)
    )
    if not complete_dates:
        raise ValueError("Treasury average interest rate API 不含完整汇总记录")
    effective_date = complete_dates[-1]
    prior_date = _prior_year_date(complete_dates, effective_date)
    latest = values[effective_date]
    prior = values[prior_date]
    marketable = latest["Total Marketable"]
    total = latest["Total Interest-bearing Debt"]
    return _snapshot(
        source_id=TREASURY_FISCAL_SOURCE_ID,
        stream_id=TREASURY_AVERAGE_INTEREST_COST_STREAM_ID,
        domain=CausalDomain.FISCAL_DEBT,
        fact_type=TREASURY_AVERAGE_INTEREST_COST_FACT_TYPE,
        effective_date=effective_date,
        headline="U.S. Treasury average interest cost",
        risk_factors=("US_FISCAL_CAPACITY",),
        metrics=(
            _metric(
                OfficialMetricName.TREASURY_MARKETABLE_AVG_INTEREST_RATE_PCT,
                marketable,
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_MARKETABLE_AVG_INTEREST_RATE_CHANGE_1Y_BPS,
                (marketable - prior["Total Marketable"]) * Decimal("100"),
                OfficialMetricUnit.BASIS_POINTS,
            ),
            _metric(
                OfficialMetricName.TREASURY_TOTAL_AVG_INTEREST_RATE_PCT,
                total,
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_TOTAL_AVG_INTEREST_RATE_CHANGE_1Y_BPS,
                (total - prior["Total Interest-bearing Debt"]) * Decimal("100"),
                OfficialMetricUnit.BASIS_POINTS,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(effective_date, observed_at),
        payload_ref=payload_ref,
    )


def _parse_treasury_interest_expense(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    document = _json_object(content)
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ValueError("Treasury interest expense API 缺少 data")
    by_date: dict[date, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        record_date = _date(row.get("record_date"), name="interest expense record_date")
        if record_date <= observed_at.date():
            by_date.setdefault(record_date, []).append(row)
    if not by_date:
        raise ValueError("Treasury interest expense API 不含截止时点前记录")
    effective_date = max(by_date)
    prior_date = _prior_year_date(sorted(by_date), effective_date)

    def totals(record_date: date) -> tuple[Decimal, Decimal]:
        current = by_date[record_date]
        month = sum(
            (
                _decimal(row.get("month_expense_amt"), name="month interest expense")
                for row in current
            ),
            Decimal("0"),
        )
        fytd = sum(
            (
                _decimal(row.get("fytd_expense_amt"), name="FYTD interest expense")
                for row in current
            ),
            Decimal("0"),
        )
        return month, fytd

    month, fytd = totals(effective_date)
    prior_month, prior_fytd = totals(prior_date)
    if prior_month == 0 or prior_fytd == 0:
        raise ValueError("Treasury interest expense 同期基数不能为零")
    return _snapshot(
        source_id=TREASURY_FISCAL_SOURCE_ID,
        stream_id=TREASURY_INTEREST_EXPENSE_STREAM_ID,
        domain=CausalDomain.FISCAL_DEBT,
        fact_type=TREASURY_INTEREST_EXPENSE_FACT_TYPE,
        effective_date=effective_date,
        headline="U.S. Treasury interest expense burden",
        risk_factors=("US_FISCAL_CAPACITY",),
        metrics=(
            _metric(
                OfficialMetricName.TREASURY_INTEREST_EXPENSE_MONTH_USD_M,
                month / Decimal("1000000"),
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.TREASURY_INTEREST_EXPENSE_MONTH_CHANGE_1Y_PCT,
                (month / prior_month - 1) * Decimal("100"),
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_INTEREST_EXPENSE_FYTD_USD_M,
                fytd / Decimal("1000000"),
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.TREASURY_INTEREST_EXPENSE_FYTD_CHANGE_1Y_PCT,
                (fytd / prior_fytd - 1) * Decimal("100"),
                OfficialMetricUnit.PERCENT,
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(effective_date, observed_at),
        payload_ref=payload_ref,
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
    entries, published_at = _treasury_curve_entries(
        content,
        name="Treasury yield",
    )
    latest_date, latest = entries[-1]
    previous = entries[-2][1]
    y2 = _decimal(latest.get("BC_2YEAR"), name="Treasury 2Y")
    y10 = _decimal(latest.get("BC_10YEAR"), name="Treasury 10Y")
    y30 = _decimal(latest.get("BC_30YEAR"), name="Treasury 30Y")
    previous_y2 = _decimal(previous.get("BC_2YEAR"), name="previous Treasury 2Y")
    previous_y10 = _decimal(previous.get("BC_10YEAR"), name="previous Treasury 10Y")
    previous_y30 = _decimal(previous.get("BC_30YEAR"), name="previous Treasury 30Y")
    two_year_history = tuple(
        (record_date, _decimal(values.get("BC_2YEAR"), name="Treasury 2Y"))
        for record_date, values in entries
    )
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
            _metric(
                OfficialMetricName.TREASURY_2Y_CHANGE_1D_BPS,
                (y2 - previous_y2) * 100,
                OfficialMetricUnit.BASIS_POINTS,
            ),
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
            two_year_history,
            candidates=(
                (
                    OfficialMetricName.TREASURY_2Y_CHANGE_1D_BPS,
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


def _parse_treasury_real_yields(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    entries, published_at = _treasury_curve_entries(
        content,
        name="Treasury real yield",
    )
    latest_date, latest = entries[-1]
    previous = entries[-2][1]
    real_5y = _decimal(latest.get("TC_5YEAR"), name="Treasury real 5Y")
    real_10y = _decimal(latest.get("TC_10YEAR"), name="Treasury real 10Y")
    real_30y = _decimal(latest.get("TC_30YEAR"), name="Treasury real 30Y")
    previous_real_10y = _decimal(
        previous.get("TC_10YEAR"),
        name="previous Treasury real 10Y",
    )
    ten_year_history = tuple(
        (record_date, _decimal(values.get("TC_10YEAR"), name="Treasury real 10Y"))
        for record_date, values in entries
    )
    return _snapshot(
        source_id=TREASURY_RATES_SOURCE_ID,
        stream_id=TREASURY_REAL_YIELD_STREAM_ID,
        domain=CausalDomain.CROSS_ASSET_EXTERNAL,
        fact_type=TREASURY_REAL_YIELD_FACT_TYPE,
        effective_date=latest_date,
        headline="U.S. Treasury real yield curve",
        risk_factors=("US_REAL_INTEREST_RATES",),
        metrics=(
            _metric(
                OfficialMetricName.TREASURY_REAL_5Y_PCT,
                real_5y,
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_REAL_10Y_PCT,
                real_10y,
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_REAL_30Y_PCT,
                real_30y,
                OfficialMetricUnit.PERCENT,
            ),
            _metric(
                OfficialMetricName.TREASURY_REAL_10Y_CHANGE_1D_BPS,
                (real_10y - previous_real_10y) * 100,
                OfficialMetricUnit.BASIS_POINTS,
            ),
        ),
        change_context=_most_unusual_change_context(
            ten_year_history,
            candidates=(
                (
                    OfficialMetricName.TREASURY_REAL_10Y_CHANGE_1D_BPS,
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


def _treasury_curve_entries(
    content: bytes,
    *,
    name: str,
) -> tuple[tuple[tuple[date, dict[str, str]], ...], datetime]:
    root = _xml(content, name=f"{name} XML")
    atom = "{http://www.w3.org/2005/Atom}"
    metadata = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
    entries: list[tuple[date, dict[str, str]]] = []
    for entry in root.findall(f"{atom}entry"):
        properties = entry.find(f".//{metadata}properties")
        if properties is None:
            continue
        values = {item.tag.rsplit("}", 1)[-1]: (item.text or "").strip() for item in properties}
        entries.append((_datetime(values.get("NEW_DATE"), name=f"{name} date").date(), values))
    entries.sort(key=lambda item: item[0])
    if len(entries) < 2:
        raise ValueError(f"{name} XML 缺少至少两个交易日")
    return tuple(entries), _datetime(root.findtext(f"{atom}updated"), name=f"{name} updated")


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


def _parse_fred_sp500(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    return _parse_fred_series(
        content,
        series_id="SP500",
        stream_id=FRED_SP500_STREAM_ID,
        fact_type=US_EQUITY_MARKET_FACT_TYPE,
        headline="FRED-hosted S&P 500 daily close",
        risk_factor="US_EQUITY_RISK_APPETITE",
        level_name=OfficialMetricName.SP500_INDEX,
        level_unit=OfficialMetricUnit.INDEX,
        change_name=OfficialMetricName.SP500_CHANGE_1D_PCT,
        change_unit=OfficialMetricUnit.PERCENT,
        percentage_change=True,
        source_url=source_url,
        observed_at=observed_at,
        payload_ref=payload_ref,
    )


def _parse_fred_high_yield_oas(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    return _parse_fred_series(
        content,
        series_id="BAMLH0A0HYM2",
        stream_id=FRED_HIGH_YIELD_OAS_STREAM_ID,
        fact_type=US_HIGH_YIELD_CREDIT_FACT_TYPE,
        headline="FRED-hosted U.S. high-yield option-adjusted spread",
        risk_factor="US_HIGH_YIELD_CREDIT_RISK",
        level_name=OfficialMetricName.US_HIGH_YIELD_OAS_PCT,
        level_unit=OfficialMetricUnit.PERCENT,
        change_name=OfficialMetricName.US_HIGH_YIELD_OAS_CHANGE_1D_BPS,
        change_unit=OfficialMetricUnit.BASIS_POINTS,
        change_multiplier=Decimal("100"),
        source_url=source_url,
        observed_at=observed_at,
        payload_ref=payload_ref,
    )


def _parse_fred_wti(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    return _parse_fred_series(
        content,
        series_id="DCOILWTICO",
        stream_id=FRED_WTI_STREAM_ID,
        fact_type=US_WTI_OIL_FACT_TYPE,
        headline="FRED-hosted WTI spot price",
        risk_factor="US_ENERGY_INFLATION",
        level_name=OfficialMetricName.WTI_USD_PER_BARREL,
        level_unit=OfficialMetricUnit.USD_PER_BARREL,
        change_name=OfficialMetricName.WTI_CHANGE_1D_PCT,
        change_unit=OfficialMetricUnit.PERCENT,
        percentage_change=True,
        source_url=source_url,
        observed_at=observed_at,
        payload_ref=payload_ref,
    )


def _parse_stablecoin_supply(
    content: bytes, *, source_url: str, observed_at: datetime, payload_ref: str
) -> OfficialMetricSnapshot:
    """Project the latest completed UTC day of aggregate USD stablecoin supply.

    The current UTC row can still change while DefiLlama refreshes underlying
    chains.  Excluding it gives replay and production the same finalized-day
    semantics.  Aggregate supply is liquidity capacity, not a directional
    crypto-return signal; downstream mechanisms must still verify where the
    liquidity went and whether prices responded.
    """

    try:
        document = json.loads(content, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("DefiLlama 稳定币供给 JSON 非法") from exc
    if not isinstance(document, list):
        raise ValueError("DefiLlama 稳定币供给 JSON 必须为数组")

    by_date: dict[date, Decimal] = {}
    for raw in document:
        if not isinstance(raw, dict):
            raise ValueError("DefiLlama 稳定币供给条目必须为对象")
        raw_timestamp = raw.get("date")
        if not isinstance(raw_timestamp, (str, int)) or isinstance(raw_timestamp, bool):
            raise ValueError("DefiLlama 稳定币供给日期非法")
        try:
            timestamp = int(raw_timestamp)
            timestamp_at = datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("DefiLlama 稳定币供给日期非法") from exc
        if timestamp_at.time() != time.min:
            raise ValueError("DefiLlama 稳定币供给日期必须是 UTC 日界")
        effective_date = timestamp_at.date()
        if effective_date > observed_at.date():
            raise ValueError("DefiLlama 稳定币供给包含未来日期")
        if effective_date == observed_at.date():
            continue
        circulating = raw.get("totalCirculating")
        if not isinstance(circulating, dict) or "peggedUSD" not in circulating:
            raise ValueError("DefiLlama 稳定币供给缺少 peggedUSD")
        value = _decimal(
            circulating["peggedUSD"],
            name="DefiLlama totalCirculating.peggedUSD",
        )
        if value <= 0:
            raise ValueError("DefiLlama 美元稳定币供给必须为正数")
        if effective_date in by_date:
            raise ValueError("DefiLlama 稳定币供给包含重复日期")
        by_date[effective_date] = value / Decimal("1000000")

    observations = tuple(sorted(by_date.items()))
    if len(observations) < 61:
        raise ValueError("DefiLlama 稳定币供给缺少至少 61 个已完成日")
    recent = observations[-61:]
    if any(current[0] - previous[0] != timedelta(days=1) for previous, current in pairwise(recent)):
        raise ValueError("DefiLlama 稳定币供给最近 61 日不连续")

    latest_date, latest = observations[-1]
    changes = {distance: latest - observations[-1 - distance][1] for distance in (1, 7, 30)}
    return _snapshot(
        source_id=DEFILLAMA_SOURCE_ID,
        source_tier=SourceTier.AGGREGATOR,
        stream_id=STABLECOIN_SUPPLY_STREAM_ID,
        domain=CausalDomain.ONCHAIN_SUPPLY,
        fact_type=USD_STABLECOIN_SUPPLY_FACT_TYPE,
        effective_date=latest_date,
        headline="DefiLlama aggregated circulating USD stablecoin supply",
        risk_factors=("CRYPTO_LIQUIDITY_CAPACITY",),
        metrics=(
            _metric(
                OfficialMetricName.USD_STABLECOIN_SUPPLY_USD_M,
                latest,
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.USD_STABLECOIN_SUPPLY_CHANGE_1D_USD_M,
                changes[1],
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.USD_STABLECOIN_SUPPLY_CHANGE_7D_USD_M,
                changes[7],
                OfficialMetricUnit.USD_MILLIONS,
            ),
            _metric(
                OfficialMetricName.USD_STABLECOIN_SUPPLY_CHANGE_30D_USD_M,
                changes[30],
                OfficialMetricUnit.USD_MILLIONS,
            ),
        ),
        change_context=_most_unusual_change_context(
            observations,
            candidates=(
                (
                    OfficialMetricName.USD_STABLECOIN_SUPPLY_CHANGE_1D_USD_M,
                    1,
                    OfficialMetricUnit.USD_MILLIONS,
                ),
                (
                    OfficialMetricName.USD_STABLECOIN_SUPPLY_CHANGE_7D_USD_M,
                    7,
                    OfficialMetricUnit.USD_MILLIONS,
                ),
                (
                    OfficialMetricName.USD_STABLECOIN_SUPPLY_CHANGE_30D_USD_M,
                    30,
                    OfficialMetricUnit.USD_MILLIONS,
                ),
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        # The API does not expose a row-level publication timestamp.  First
        # observation is the earliest honest point-in-time availability bound.
        source_published_at=observed_at,
        payload_ref=payload_ref,
    )


def _parse_fred_series(
    content: bytes,
    *,
    series_id: str,
    stream_id: str,
    fact_type: str,
    headline: str,
    risk_factor: str,
    level_name: OfficialMetricName,
    level_unit: OfficialMetricUnit,
    change_name: OfficialMetricName,
    change_unit: OfficialMetricUnit,
    source_url: str,
    observed_at: datetime,
    payload_ref: str,
    change_multiplier: Decimal = Decimal("1"),
    percentage_change: bool = False,
) -> OfficialMetricSnapshot:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"FRED {series_id} CSV 编码非法") from exc
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames != ["observation_date", series_id]:
        raise ValueError(f"FRED {series_id} CSV 表头与固定合同不一致")
    values: dict[date, Decimal] = {}
    for row in reader:
        raw_value = (row.get(series_id) or "").strip()
        if not raw_value or raw_value == ".":
            continue
        effective_date = _date(
            row.get("observation_date"),
            name=f"FRED {series_id} observation_date",
        )
        if effective_date > observed_at.date():
            raise ValueError(f"FRED {series_id} 包含未来观测")
        values[effective_date] = _decimal(raw_value, name=f"FRED {series_id} value")
    observations = tuple(sorted(values.items()))
    if len(observations) < 30:
        raise ValueError(f"FRED {series_id} 缺少至少 30 条有效观测")
    latest_date, latest = observations[-1]
    previous = observations[-2][1]
    latest_change = (
        (latest / previous - 1) * Decimal("100")
        if percentage_change
        else (latest - previous) * change_multiplier
    )
    return _snapshot(
        source_id=FRED_SOURCE_ID,
        source_tier=SourceTier.AGGREGATOR,
        stream_id=stream_id,
        domain=CausalDomain.CROSS_ASSET_EXTERNAL,
        fact_type=fact_type,
        effective_date=latest_date,
        headline=headline,
        risk_factors=(risk_factor,),
        metrics=(
            _metric(level_name, latest, level_unit),
            _metric(change_name, latest_change, change_unit),
        ),
        change_context=_most_unusual_change_context(
            observations,
            candidates=(
                (
                    change_name,
                    1,
                    change_unit,
                    Decimal("100") if percentage_change else change_multiplier,
                    percentage_change,
                ),
            ),
        ),
        source_url=source_url,
        observed_at=observed_at,
        source_published_at=_effective_at(latest_date, observed_at),
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
        effective_date = datetime.strptime(metadata["Fund Holdings as of"], "%b %d, %Y").date()
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
                OfficialMetricName.IBIT_HOLDINGS_MARKET_VALUE_USD_M,
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
                OfficialMetricName.BTC_ETP_HOLDINGS_MARKET_VALUE_USD_M,
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
                OfficialMetricName.BTC_ETP_HOLDINGS_MARKET_VALUE_USD_M,
                market_value / Decimal("1000000"),
                OfficialMetricUnit.USD_MILLIONS,
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
        if item.stream_id == snapshot.stream_id and item.effective_date < snapshot.effective_date
    }
    ordered = tuple(by_date[key] for key in sorted(by_date))
    if not ordered:
        return snapshot
    holdings_history = tuple(
        (item.effective_date, _metric_value(item, holdings_name)) for item in (*ordered, snapshot)
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
            (item.effective_date, _metric_value(item, shares_name)) for item in (*ordered, snapshot)
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
    source_tier: SourceTier = SourceTier.FIRST_PARTY,
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
            source_tier=source_tier,
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
        OfficialMetricUnit.USD_PER_BARREL: Decimal("0.01"),
        OfficialMetricUnit.YEARS: Decimal("0.001"),
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
        percentile = Decimal(sum(abs(value) <= abs(latest) for value in changes)) / Decimal(
            len(changes)
        )
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


def _prior_year_date(values: list[date], latest: date) -> date:
    candidates = [
        value for value in values if value.year == latest.year - 1 and value.month == latest.month
    ]
    if not candidates:
        raise ValueError("官方月度指标缺少上年同期")
    return max(candidates)


def _stream_source_id(stream_id: str) -> str:
    if stream_id in {
        TGA_STREAM_ID,
        TREASURY_REFINANCING_STREAM_ID,
        TREASURY_AVERAGE_INTEREST_COST_STREAM_ID,
        TREASURY_INTEREST_EXPENSE_STREAM_ID,
    }:
        return TREASURY_FISCAL_SOURCE_ID
    if stream_id == TREASURY_AUCTION_STREAM_ID:
        return TREASURY_DIRECT_SOURCE_ID
    if stream_id in {TREASURY_REAL_YIELD_STREAM_ID, TREASURY_YIELD_STREAM_ID}:
        return TREASURY_RATES_SOURCE_ID
    if stream_id == FED_BROAD_DOLLAR_STREAM_ID:
        return FED_H10_SOURCE_ID
    if stream_id in {
        FRED_SP500_STREAM_ID,
        FRED_HIGH_YIELD_OAS_STREAM_ID,
        FRED_WTI_STREAM_ID,
    }:
        return FRED_SOURCE_ID
    if stream_id == STABLECOIN_SUPPLY_STREAM_ID:
        return DEFILLAMA_SOURCE_ID
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
