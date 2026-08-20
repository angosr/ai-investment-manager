from pydantic import Field

from investment_manager.kernel.configuration import StrictConfig


class PanelPolicy(StrictConfig):
    version: str
    schema_version: str = "panel-v1"
    max_characters: int = Field(default=12_000, ge=4_000, le=12_000)
    max_evidence: int = Field(default=20, ge=0, le=100)
    max_per_source: int = Field(default=5, ge=1)
    max_market_age_seconds: int = Field(default=180, ge=1)
    maximum_evidence_age_seconds: int = Field(default=86_400, ge=60, le=604_800)
