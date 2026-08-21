from pydantic import Field, model_validator

from investment_manager.kernel.configuration import StrictConfig
from investment_manager.kernel.types import FrozenModel
from investment_manager.state.facts import (
    FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
    FED_MONETARY_RELEASE_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    OfficialFactProjectionPolicy,
    StateDeltaPolicy,
)


class DecisionPacketPolicy(FrozenModel):
    version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    maximum_facts: int = Field(default=12, ge=1, le=50)
    maximum_fact_characters: int = Field(default=4_000, ge=500, le=10_000)
    maximum_characters_per_fact: int = Field(default=600, ge=100, le=1_200)
    maximum_background_fact_distance_seconds: int = Field(
        default=172_800,
        ge=3_600,
        le=2_592_000,
    )
    maximum_intelligence_events: int = Field(default=8, ge=0, le=20)
    maximum_intelligence_characters: int = Field(default=3_000, ge=0, le=6_000)
    maximum_characters_per_intelligence_event: int = Field(
        default=500,
        ge=100,
        le=1_000,
    )
    maximum_packet_characters: int = Field(default=12_000, ge=2_000, le=16_000)
    maximum_previous_context_drivers: int = Field(default=3, ge=0, le=8)


class PanelPolicy(StrictConfig):
    version: str
    schema_version: str = "panel-v1"
    max_characters: int = Field(default=12_000, ge=4_000, le=12_000)
    max_evidence: int = Field(default=20, ge=0, le=100)
    max_per_source: int = Field(default=5, ge=1)
    max_market_age_seconds: int = Field(default=180, ge=1)
    maximum_evidence_age_seconds: int = Field(default=86_400, ge=60, le=604_800)


class DecisionStatePolicy(StrictConfig):
    """Versioned point-in-time projection and Packet capacity contract."""

    version: str
    analysis_scope: str
    official_fact_policy: OfficialFactProjectionPolicy
    delta_policy: StateDeltaPolicy
    packet_policy: DecisionPacketPolicy

    @model_validator(mode="after")
    def official_projection_must_have_delta_rules(self):
        if not self.official_fact_policy.affected_assets:
            raise ValueError("OfficialFact projection 必须声明受影响资产")
        required = {
            FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
            FED_MONETARY_RELEASE_FACT_TYPE,
            FOMC_MEETING_FACT_TYPE,
        }
        configured = {item.fact_type for item in self.delta_policy.rules}
        if not required.issubset(configured):
            raise ValueError("OfficialFact projection 缺少对应 MaterialDelta 规则")
        return self
