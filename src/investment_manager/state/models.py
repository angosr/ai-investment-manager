from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import DomainCoverageSnapshot
from investment_manager.kernel.identity import SHA256_PATTERN
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel


class FactRevisionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    RETRACTED = "RETRACTED"
    CONFLICTED = "CONFLICTED"


class DeltaCategory(StrEnum):
    FIRST_PARTY_FACT = "FIRST_PARTY_FACT"
    INTELLIGENCE_EVENT = "INTELLIGENCE_EVENT"
    MARKET = "MARKET"
    DERIVATIVES = "DERIVATIVES"
    CROSS_ASSET = "CROSS_ASSET"
    PORTFOLIO = "PORTFOLIO"
    DATA_QUALITY = "DATA_QUALITY"


class Materiality(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class CanonicalFactRevision(FrozenModel):
    fact_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    previous_revision_id: str | None = Field(default=None, min_length=1)
    projection_version: str = Field(min_length=1)
    fact_type: str = Field(min_length=1, max_length=80)
    status: FactRevisionStatus
    event_time: datetime | None = None
    observed_at: datetime
    headline: str = Field(min_length=1, max_length=500)
    claim: str = Field(min_length=1, max_length=2_000)
    affected_assets: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = Field(min_length=1)
    source_observation_ids: tuple[str, ...] = Field(min_length=1)
    revision_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_event_time = field_validator("event_time")(optional_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def revision_identity_and_refs_must_be_consistent(self):
        if self.previous_revision_id == self.revision_id:
            raise ValueError("事实修订不能引用自身")
        if len(set(self.source_observation_ids)) != len(self.source_observation_ids):
            raise ValueError("事实修订不能重复引用来源观测")
        if tuple(sorted(set(self.affected_assets))) != self.affected_assets:
            raise ValueError("affected_assets 必须唯一且排序")
        if tuple(sorted(set(self.risk_factors))) != self.risk_factors:
            raise ValueError("risk_factors 必须唯一且排序")
        return self


class StateSnapshot(FrozenModel):
    state_id: str = Field(min_length=1)
    projection_version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    as_of: datetime
    built_at: datetime
    fact_revision_ids: tuple[str, ...] = ()
    market_snapshot_refs: tuple[str, ...] = ()
    feature_snapshot_refs: tuple[str, ...] = ()
    derivative_snapshot_refs: tuple[str, ...] = ()
    intelligence_event_refs: tuple[str, ...] = ()
    account_snapshot_ref: str | None = Field(default=None, min_length=1)
    data_quality_codes: tuple[str, ...] = ()
    coverage_gap_codes: tuple[str, ...] = ()
    information_coverage: tuple[DomainCoverageSnapshot, ...] = ()
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_built_at = field_validator("built_at")(require_utc)

    @model_validator(mode="after")
    def snapshot_must_be_deterministic_and_point_in_time_safe(self):
        if self.built_at < self.as_of:
            raise ValueError("StateSnapshot built_at 不能早于 as_of")
        for name in (
            "fact_revision_ids",
            "market_snapshot_refs",
            "feature_snapshot_refs",
            "derivative_snapshot_refs",
            "intelligence_event_refs",
            "data_quality_codes",
            "coverage_gap_codes",
        ):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} 必须唯一且排序")
        coverage_domains = tuple(item.domain.value for item in self.information_coverage)
        if tuple(sorted(set(coverage_domains))) != coverage_domains:
            raise ValueError("StateSnapshot information_coverage 必须按领域唯一且排序")
        if any(item.as_of != self.as_of for item in self.information_coverage):
            raise ValueError("StateSnapshot information_coverage 必须与 state as_of 一致")
        return self


class MaterialDelta(FrozenModel):
    delta_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    previous_state_id: str | None = Field(default=None, min_length=1)
    current_state_id: str = Field(min_length=1)
    observed_at: datetime
    expires_at: datetime
    category: DeltaCategory
    materiality: Materiality
    affected_assets: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = Field(min_length=1)
    horizons_minutes: tuple[int, ...] = Field(min_length=1)
    fact_revision_ids: tuple[str, ...] = ()
    feature_snapshot_refs: tuple[str, ...] = ()
    intelligence_event_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_expires_at = field_validator("expires_at")(require_utc)

    @model_validator(mode="after")
    def delta_must_reference_a_real_bounded_change(self):
        if self.previous_state_id == self.current_state_id:
            raise ValueError("MaterialDelta 必须比较不同状态")
        if self.observed_at >= self.expires_at:
            raise ValueError("MaterialDelta 必须具有未来有效期")
        for name in (
            "affected_assets",
            "risk_factors",
            "horizons_minutes",
            "fact_revision_ids",
            "feature_snapshot_refs",
            "intelligence_event_refs",
            "reason_codes",
        ):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} 必须唯一且排序")
        if any(value <= 0 for value in self.horizons_minutes):
            raise ValueError("horizons_minutes 必须全部为正数")
        if (
            not self.fact_revision_ids
            and not self.feature_snapshot_refs
            and not self.intelligence_event_refs
        ):
            raise ValueError("MaterialDelta 必须引用事实、特征或事件变化")
        return self
