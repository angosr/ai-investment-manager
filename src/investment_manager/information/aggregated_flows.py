from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from investment_manager.information.models import (
    CausalDomain,
    SourceObservation,
    SourceTier,
)
from investment_manager.information.official.metrics import OFFICIAL_METRIC_FACT_TYPES
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

BYKARANTELI_SOURCE_ID = "bykaranteli"
ETF_AGGREGATE_FLOW_STREAM_ID = "bykaranteli-etf-aggregate-flows"
ETF_AGGREGATE_FLOW_URL = (
    "https://bykaranteli.com/api/v1/public/datasets/etf-flows.json"
)
BTC_ETF_AGGREGATE_FLOW_FACT_TYPE = "BTC_ETF_AGGREGATE_FLOW_SNAPSHOT"
ETH_ETF_AGGREGATE_FLOW_FACT_TYPE = "ETH_ETF_AGGREGATE_FLOW_SNAPSHOT"
AGGREGATED_FLOW_RISK_FACTORS_BY_TYPE = {
    BTC_ETF_AGGREGATE_FLOW_FACT_TYPE: frozenset({"BTC_INSTITUTIONAL_FLOW"}),
    ETH_ETF_AGGREGATE_FLOW_FACT_TYPE: frozenset({"ETH_INSTITUTIONAL_FLOW"}),
}
AGGREGATED_FLOW_FACT_TYPES = frozenset(AGGREGATED_FLOW_RISK_FACTORS_BY_TYPE)
CONTINUOUS_CONTEXT_FACT_TYPES = OFFICIAL_METRIC_FACT_TYPES | AGGREGATED_FLOW_FACT_TYPES
_USD_MILLION = Decimal("1000000")


class AggregatedRecordKind(StrEnum):
    ETF_FLOW_SNAPSHOT = "AGGREGATED_ETF_FLOW_SNAPSHOT"


class _EtfFlowRow(FrozenModel):
    date: date
    asset: Literal["BTC", "ETH"]
    net_inflow_usd: Decimal
    net_assets_usd: Decimal = Field(gt=0)
    cumulative_inflow_usd: Decimal
    value_traded_usd: Decimal = Field(ge=0)
    source: Literal["bykaranteli.com"]


class _EtfFlowDataset(FrozenModel):
    dataset: Literal["etf-flows"]
    count: int = Field(gt=0)
    rows: tuple[_EtfFlowRow, ...] = Field(min_length=60)

    @model_validator(mode="after")
    def count_and_rows_must_be_consistent(self):
        if self.count != len(self.rows):
            raise ValueError("ETF 合计流数据集 count 与 rows 不一致")
        keys = tuple((item.asset, item.date) for item in self.rows)
        if len(set(keys)) != len(keys):
            raise ValueError("ETF 合计流数据集包含重复资产日期")
        if {item.asset for item in self.rows} != {"BTC", "ETH"}:
            raise ValueError("ETF 合计流数据集必须同时覆盖 BTC 与 ETH")
        return self


class AggregatedEtfFlowSnapshot(FrozenModel):
    """One point-in-time aggregate flow observation from a named aggregator."""

    observation: SourceObservation
    kind: Literal[AggregatedRecordKind.ETF_FLOW_SNAPSHOT] = (
        AggregatedRecordKind.ETF_FLOW_SNAPSHOT
    )
    stream_id: Literal[ETF_AGGREGATE_FLOW_STREAM_ID] = ETF_AGGREGATE_FLOW_STREAM_ID
    domain: Literal[CausalDomain.INSTITUTIONAL_FLOWS] = (
        CausalDomain.INSTITUTIONAL_FLOWS
    )
    asset: Literal["BTC", "ETH"]
    effective_date: date
    net_inflow_usd_m: Decimal
    net_assets_usd_m: Decimal = Field(gt=0)
    cumulative_inflow_usd_m: Decimal
    value_traded_usd_m: Decimal = Field(ge=0)
    absolute_flow_percentile: Decimal = Field(ge=0, le=1)
    sample_size: int = Field(ge=30)
    lookback_start: date
    lookback_end: date
    source_url: Literal[ETF_AGGREGATE_FLOW_URL] = ETF_AGGREGATE_FLOW_URL

    @property
    def fact_type(self) -> str:
        return (
            BTC_ETF_AGGREGATE_FLOW_FACT_TYPE
            if self.asset == "BTC"
            else ETH_ETF_AGGREGATE_FLOW_FACT_TYPE
        )

    @model_validator(mode="after")
    def identity_and_window_must_be_consistent(self):
        observation = self.observation
        if (
            observation.source_id != BYKARANTELI_SOURCE_ID
            or observation.source_tier != SourceTier.AGGREGATOR
            or observation.source_record_id
            != f"aggregated-etf-flow:{self.asset.lower()}"
        ):
            raise ValueError("ETF 合计流必须引用指定聚合来源和资产身份")
        if not self.lookback_start <= self.effective_date == self.lookback_end:
            raise ValueError("ETF 合计流回看窗口与有效日期不一致")
        if self.effective_date > observation.observed_at.date():
            raise ValueError("ETF 合计流有效日期晚于首次观察时间")
        if observation.payload_hash != content_hash(
            aggregated_etf_flow_semantic_payload(self)
        ):
            raise ValueError("ETF 合计流 observation payload_hash 不一致")
        return self


@dataclass(frozen=True, slots=True)
class AggregatedEtfFlowDocument:
    content: bytes
    source_url: str = ETF_AGGREGATE_FLOW_URL
    media_type: str = "application/json"


class HttpAggregatedEtfFlowSource:
    """Pinned public aggregate-flow endpoint; no browser scraping or fallback proxy."""

    def __init__(self, *, timeout_seconds: int) -> None:
        if timeout_seconds < 1:
            raise ValueError("ETF 合计流请求超时必须为正数")
        self._timeout_seconds = timeout_seconds

    def fetch(self, *, observed_at: datetime) -> AggregatedEtfFlowDocument:
        import httpx

        require_utc(observed_at)
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "investment-manager/aggregated-etf-flow-v1"},
        ) as client:
            response = client.get(ETF_AGGREGATE_FLOW_URL)
        response.raise_for_status()
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if media_type != "application/json" or not response.content:
            raise ValueError("ETF 合计流端点未返回非空 JSON")
        return AggregatedEtfFlowDocument(content=response.content)


def parse_aggregated_etf_flows(
    document: AggregatedEtfFlowDocument,
    *,
    observed_at: datetime,
) -> tuple[AggregatedEtfFlowSnapshot, ...]:
    observed_at = require_utc(observed_at)
    if document.source_url != ETF_AGGREGATE_FLOW_URL:
        raise ValueError("ETF 合计流来源 URL 未固定")
    try:
        raw = json.loads(document.content, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("ETF 合计流 JSON 非法") from exc
    dataset = _EtfFlowDataset.model_validate(raw)
    if any(item.date > observed_at.date() for item in dataset.rows):
        raise ValueError("ETF 合计流包含未来日期")
    raw_payload = build_raw_source_payload(
        source_id=BYKARANTELI_SOURCE_ID,
        source_url=document.source_url,
        media_type=document.media_type,
        observed_at=observed_at,
        content=document.content,
    )
    snapshots = tuple(
        _snapshot_for_asset(
            tuple(item for item in dataset.rows if item.asset == asset),
            asset=asset,
            observed_at=observed_at,
            payload_ref=raw_payload.payload_id,
        )
        for asset in ("BTC", "ETH")
    )
    return tuple(sorted(snapshots, key=lambda item: item.asset))


def aggregated_etf_flow_semantic_payload(
    snapshot: AggregatedEtfFlowSnapshot,
) -> dict[str, object]:
    return snapshot.model_dump(
        mode="json",
        exclude={"observation"},
    )


def _snapshot_for_asset(
    rows: tuple[_EtfFlowRow, ...],
    *,
    asset: Literal["BTC", "ETH"],
    observed_at: datetime,
    payload_ref: str,
) -> AggregatedEtfFlowSnapshot:
    ordered = tuple(sorted(rows, key=lambda item: item.date))
    if len(ordered) < 30:
        raise ValueError(f"{asset} ETF 合计流历史不足 30 个交易日")
    latest = ordered[-1]
    percentile = Decimal(
        sum(abs(item.net_inflow_usd) <= abs(latest.net_inflow_usd) for item in ordered)
    ) / Decimal(len(ordered))
    draft = AggregatedEtfFlowSnapshot.model_construct(
        observation=SourceObservation.model_construct(
            observation_id="pending",
            source_id=BYKARANTELI_SOURCE_ID,
            source_tier=SourceTier.AGGREGATOR,
            source_record_id=f"aggregated-etf-flow:{asset.lower()}",
            observed_at=observed_at,
            # The endpoint does not expose a publication timestamp.  First
            # observation is the earliest honest availability bound; never
            # backdate availability to the row's effective trading date.
            source_published_at=observed_at,
            payload_hash="0" * 64,
            payload_ref=payload_ref,
        ),
        asset=asset,
        effective_date=latest.date,
        net_inflow_usd_m=_usd_m(latest.net_inflow_usd),
        net_assets_usd_m=_usd_m(latest.net_assets_usd),
        cumulative_inflow_usd_m=_usd_m(latest.cumulative_inflow_usd),
        value_traded_usd_m=_usd_m(latest.value_traded_usd),
        absolute_flow_percentile=percentile.quantize(Decimal("0.0001")),
        sample_size=len(ordered),
        lookback_start=ordered[0].date,
        lookback_end=latest.date,
        source_url=ETF_AGGREGATE_FLOW_URL,
    )
    payload_hash = content_hash(aggregated_etf_flow_semantic_payload(draft))
    record_id = draft.observation.source_record_id
    observation = SourceObservation(
        **draft.observation.model_dump(exclude={"observation_id", "payload_hash"}),
        payload_hash=payload_hash,
        observation_id=stable_id(
            "source_observation",
            BYKARANTELI_SOURCE_ID,
            record_id,
            payload_hash,
            observed_at.isoformat(),
        ),
    )
    return AggregatedEtfFlowSnapshot(
        **draft.model_dump(exclude={"observation"}),
        observation=observation,
    )


def _usd_m(value: Decimal) -> Decimal:
    return (value / _USD_MILLION).quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN)
