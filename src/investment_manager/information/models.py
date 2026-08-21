from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import SHA256_PATTERN
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, UnitInterval


class SourceTier(StrEnum):
    FIRST_PARTY = "FIRST_PARTY"
    CONTRACTED = "CONTRACTED"
    AGGREGATOR = "AGGREGATOR"


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
        if (
            self.source_published_at is not None
            and self.source_published_at > self.observed_at
        ):
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
    url: str | None = None
    symbols: tuple[str, ...]
    relevance: UnitInterval
    impact: UnitInterval
    source_reliability: UnitInterval
    novelty: UnitInterval

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)
