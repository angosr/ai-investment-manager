from pydantic import Field

from investment_manager.kernel.configuration import StrictConfig
from investment_manager.kernel.types import FrozenModel
from investment_manager.state.facts import FactDeltaPolicy


class DecisionPacketPolicy(FrozenModel):
    version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    maximum_facts: int = Field(default=12, ge=1, le=50)
    maximum_fact_characters: int = Field(default=4_000, ge=500, le=10_000)
    maximum_characters_per_fact: int = Field(default=600, ge=100, le=1_200)
    maximum_packet_characters: int = Field(default=12_000, ge=2_000, le=16_000)
    maximum_active_hypotheses: int = Field(default=5, ge=0, le=20)


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
    delta_policy: FactDeltaPolicy
    packet_policy: DecisionPacketPolicy
