from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import SourceObservation, SourceTier
from investment_manager.information.official.economic_calendar import (
    BEA_CALENDAR_SOURCE_ID,
    BLS_CALENDAR_SOURCE_ID,
    EconomicReleaseEventRecord,
    EconomicReleaseKind,
)
from investment_manager.information.official.records import OfficialRecordKind
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel


class EconomicReleaseMetric(StrEnum):
    PERSONAL_INCOME_MOM_PCT = "personal_income_mom_pct"
    DISPOSABLE_PERSONAL_INCOME_MOM_PCT = "disposable_personal_income_mom_pct"
    NOMINAL_PCE_MOM_PCT = "nominal_pce_mom_pct"
    REAL_PCE_MOM_PCT = "real_pce_mom_pct"
    PCE_PRICE_MOM_PCT = "pce_price_mom_pct"
    CORE_PCE_PRICE_MOM_PCT = "core_pce_price_mom_pct"
    PCE_PRICE_YOY_PCT = "pce_price_yoy_pct"
    CORE_PCE_PRICE_YOY_PCT = "core_pce_price_yoy_pct"
    PERSONAL_SAVING_RATE_PCT = "personal_saving_rate_pct"
    REAL_GDP_QOQ_ANNUALIZED_PCT = "real_gdp_qoq_annualized_pct"
    PRIOR_GDP_VINTAGE_QOQ_ANNUALIZED_PCT = "prior_gdp_vintage_qoq_annualized_pct"
    HEADLINE_CPI_MOM_PCT = "headline_cpi_mom_pct"
    CORE_CPI_MOM_PCT = "core_cpi_mom_pct"
    HEADLINE_CPI_YOY_PCT = "headline_cpi_yoy_pct"
    CORE_CPI_YOY_PCT = "core_cpi_yoy_pct"
    NONFARM_PAYROLL_CHANGE_THOUSANDS = "nonfarm_payroll_change_thousands"
    UNEMPLOYMENT_RATE_PCT = "unemployment_rate_pct"
    AVERAGE_HOURLY_EARNINGS_MOM_PCT = "average_hourly_earnings_mom_pct"
    AVERAGE_HOURLY_EARNINGS_YOY_PCT = "average_hourly_earnings_yoy_pct"


class EconomicReleaseUnit(StrEnum):
    PERCENT = "PERCENT"
    THOUSANDS = "THOUSANDS"


class EconomicReleaseValue(FrozenModel):
    name: EconomicReleaseMetric
    value: Decimal
    unit: EconomicReleaseUnit


class EconomicReleaseActualRecord(FrozenModel):
    """First visible official values for one scheduled economic release."""

    observation: SourceObservation
    kind: Literal[OfficialRecordKind.ECONOMIC_RELEASE_ACTUAL] = (
        OfficialRecordKind.ECONOMIC_RELEASE_ACTUAL
    )
    calendar_event_id: str = Field(min_length=1)
    release_kind: EconomicReleaseKind
    scheduled_at: datetime
    period: str = Field(min_length=1, max_length=80)
    vintage: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=1_000)
    values: tuple[EconomicReleaseValue, ...] = Field(min_length=1, max_length=24)
    source_url: str = Field(min_length=1, max_length=2_000)

    _utc_scheduled_at = field_validator("scheduled_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_values_are_consistent(self):
        if self.observation.source_tier != SourceTier.FIRST_PARTY:
            raise ValueError("经济发布实际值必须来自一手来源")
        if self.observation.source_published_at != self.scheduled_at:
            raise ValueError("经济发布实际值必须绑定官方日历发布时间")
        if self.observation.observed_at < self.scheduled_at:
            raise ValueError("经济发布实际值不能在发布时间前可见")
        expected_source = {
            EconomicReleaseKind.US_CPI: BLS_CALENDAR_SOURCE_ID,
            EconomicReleaseKind.US_EMPLOYMENT: BLS_CALENDAR_SOURCE_ID,
            EconomicReleaseKind.US_GDP: BEA_CALENDAR_SOURCE_ID,
            EconomicReleaseKind.US_PCE: BEA_CALENDAR_SOURCE_ID,
        }[self.release_kind]
        if self.observation.source_id != expected_source:
            raise ValueError("经济发布实际值来源与发布类型不一致")
        host = urlparse(self.source_url).hostname
        allowed_hosts = (
            {"api.bls.gov"} if expected_source == BLS_CALENDAR_SOURCE_ID else {"www.bea.gov"}
        )
        if urlparse(self.source_url).scheme != "https" or host not in allowed_hosts:
            raise ValueError("经济发布实际值 URL 不属于固定官方来源")
        names = tuple(item.name.value for item in self.values)
        if tuple(sorted(set(names))) != names:
            raise ValueError("经济发布实际值必须按指标唯一排序")
        expected_record_id = f"economic-release-actual:{self.calendar_event_id}"
        if self.observation.source_record_id != expected_record_id:
            raise ValueError("经济发布实际值记录身份与日历事件不一致")
        expected_hash = content_hash(economic_actual_semantic_payload(self))
        if self.observation.payload_hash != expected_hash:
            raise ValueError("经济发布实际值 payload_hash 与内容不一致")
        expected_id = stable_id(
            "source_observation",
            self.observation.source_id,
            expected_record_id,
            expected_hash,
            self.observation.observed_at.isoformat(),
        )
        if self.observation.observation_id != expected_id:
            raise ValueError("经济发布实际值 observation_id 与内容不一致")
        return self


def economic_calendar_event_id(event: EconomicReleaseEventRecord) -> str:
    return stable_id(
        "market_calendar_event",
        event.observation.source_id,
        event.observation.source_record_id,
    )


def economic_actual_semantic_payload(record: EconomicReleaseActualRecord) -> dict:
    return {
        "calendar_event_id": record.calendar_event_id,
        "release_kind": record.release_kind.value,
        "scheduled_at": record.scheduled_at.isoformat(),
        "period": record.period,
        "vintage": record.vintage,
        "title": record.title,
        "values": record.values,
        "source_url": record.source_url,
    }
