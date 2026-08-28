from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AliasChoices, Field, field_validator, model_validator

from investment_manager.kernel.identity import SHA256_PATTERN, content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, UnitInterval


class SourceTier(StrEnum):
    FIRST_PARTY = "FIRST_PARTY"
    CONTRACTED = "CONTRACTED"
    AGGREGATOR = "AGGREGATOR"


class CausalDomain(StrEnum):
    """Decision-relevant world layers; sources map here, prompts do not invent them."""

    FISCAL_DEBT = "FISCAL_DEBT"
    MONETARY_INFLATION = "MONETARY_INFLATION"
    REGULATION_LEGISLATION = "REGULATION_LEGISLATION"
    INSTITUTIONAL_FLOWS = "INSTITUTIONAL_FLOWS"
    SPOT_DERIVATIVES = "SPOT_DERIVATIVES"
    ONCHAIN_SUPPLY = "ONCHAIN_SUPPLY"
    CROSS_ASSET_EXTERNAL = "CROSS_ASSET_EXTERNAL"


class SourcePollStatus(StrEnum):
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class CoverageStatus(StrEnum):
    CURRENT = "CURRENT"
    PARTIAL = "PARTIAL"
    NO_RECENT_PUBLICATION = "NO_RECENT_PUBLICATION"
    SOURCE_STALE = "SOURCE_STALE"
    SOURCE_FAILED = "SOURCE_FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class SourcePollRecord(FrozenModel):
    poll_id: str = Field(min_length=1)
    source_stream_id: str = Field(min_length=1, max_length=128)
    domain: CausalDomain
    status: SourcePollStatus
    started_at: datetime
    completed_at: datetime
    poll_fresh_until: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    latest_publication_at: datetime | None = None
    valid_until: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    observation_count: int = Field(default=0, ge=0)
    new_fact_count: int = Field(default=0, ge=0)
    error_class: str | None = Field(default=None, min_length=1, max_length=200)

    _utc_started_at = field_validator("started_at")(require_utc)
    _utc_completed_at = field_validator("completed_at")(require_utc)
    _utc_poll_fresh_until = field_validator("poll_fresh_until")(optional_utc)
    _utc_latest_publication_at = field_validator("latest_publication_at")(optional_utc)
    _utc_valid_until = field_validator("valid_until")(optional_utc)

    @model_validator(mode="after")
    def outcome_must_be_consistent(self):
        if self.completed_at < self.started_at:
            raise ValueError("来源轮询完成时间不能早于开始时间")
        if (
            self.poll_fresh_until is not None
            and self.poll_fresh_until <= self.completed_at
        ):
            raise ValueError("来源轮询新鲜期必须晚于完成时间")
        if (
            self.latest_publication_at is not None
            and self.latest_publication_at > self.completed_at
        ):
            raise ValueError("来源最新发布时间不能晚于轮询完成时间")
        if self.status == SourcePollStatus.FAILED:
            if (
                self.error_class is None
                or self.observation_count
                or self.new_fact_count
                or self.poll_fresh_until is not None
                or self.valid_until is not None
            ):
                raise ValueError("失败轮询必须只记录错误类型")
        elif self.error_class is not None:
            raise ValueError("成功轮询不得记录错误类型")
        payload = self.model_dump(exclude={"poll_id"})
        if self.poll_id != stable_id("source_poll", content_hash(payload)):
            raise ValueError("来源轮询身份与内容不一致")
        return self


class DomainCoverageSnapshot(FrozenModel):
    domain: CausalDomain
    status: CoverageStatus
    as_of: datetime
    source_stream_ids: tuple[str, ...] = ()
    covered_capabilities: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    missing_capabilities: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    latest_success_at: datetime | None = None
    latest_publication_at: datetime | None = None
    latest_poll_refs: tuple[str, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_latest_success_at = field_validator("latest_success_at")(optional_utc)
    _utc_latest_publication_at = field_validator("latest_publication_at")(optional_utc)

    @model_validator(mode="after")
    def point_in_time_and_refs_must_be_consistent(self):
        if tuple(sorted(set(self.source_stream_ids))) != self.source_stream_ids:
            raise ValueError("coverage source_stream_ids 必须唯一且排序")
        if tuple(sorted(set(self.latest_poll_refs))) != self.latest_poll_refs:
            raise ValueError("coverage latest_poll_refs 必须唯一且排序")
        if tuple(sorted(set(self.covered_capabilities))) != self.covered_capabilities:
            raise ValueError("coverage covered_capabilities 必须唯一且排序")
        if tuple(sorted(set(self.missing_capabilities))) != self.missing_capabilities:
            raise ValueError("coverage missing_capabilities 必须唯一且排序")
        if set(self.covered_capabilities) & set(self.missing_capabilities):
            raise ValueError("coverage 已覆盖与缺失能力不得重叠")
        if self.latest_success_at is not None and self.latest_success_at > self.as_of:
            raise ValueError("coverage 成功时间不能晚于 as_of")
        if self.latest_publication_at is not None and self.latest_publication_at > self.as_of:
            raise ValueError("coverage 发布时间不能晚于 as_of")
        if self.status == CoverageStatus.NOT_CONFIGURED and (
            self.source_stream_ids
            or self.covered_capabilities
            or self.latest_success_at is not None
            or self.latest_publication_at is not None
            or self.latest_poll_refs
        ):
            raise ValueError("未配置领域不得伪造来源覆盖")
        if self.status == CoverageStatus.PARTIAL and not self.missing_capabilities:
            raise ValueError("部分覆盖领域必须声明缺失能力")
        if self.status == CoverageStatus.CURRENT and self.missing_capabilities:
            raise ValueError("完整覆盖领域不得声明缺失能力")
        return self


class SourceObservation(FrozenModel):
    observation_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_tier: SourceTier
    source_record_id: str = Field(min_length=1)
    observed_at: datetime
    source_published_at: datetime | None = None
    payload_hash: str = Field(pattern=SHA256_PATTERN)
    payload_ref: str = Field(min_length=1)

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_source_published_at = field_validator("source_published_at")(optional_utc)

    @model_validator(mode="after")
    def publication_must_be_point_in_time_visible(self):
        if self.source_published_at is not None and self.source_published_at > self.observed_at:
            raise ValueError("来源发布时间不能晚于系统观察时间")
        return self


class IntelligenceEvent(FrozenModel):
    evidence_id: str
    normalizer_version: str = "legacy-unknown"
    acquisition_route: str = "legacy-unknown"
    event_time: datetime
    observed_at: datetime
    source: str
    title: str
    body: str
    decision_excerpt: str = Field(default="", max_length=2_000)
    url: str | None = None
    symbols: tuple[str, ...]
    relevance: UnitInterval
    attention_priority: UnitInterval = Field(
        validation_alias=AliasChoices("attention_priority", "impact")
    )
    source_reliability: UnitInterval
    novelty: UnitInterval
    immediate_review_eligible: bool = False
    directional_support_eligible: bool = False

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @property
    def trigger_priority(self) -> int:
        """Only a qualified lead may turn discovery rank into scheduling priority."""

        if not self.immediate_review_eligible:
            return 0
        return int(self.attention_priority * 100)
