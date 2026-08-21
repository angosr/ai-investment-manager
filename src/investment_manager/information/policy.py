import re

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import CausalDomain
from investment_manager.kernel.configuration import StrictConfig


class CoverageRequirement(StrictConfig):
    domain: CausalDomain
    source_stream_ids: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    source_capabilities: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    maximum_poll_age_seconds: int = Field(default=300, ge=10, le=604_800)
    maximum_publication_age_seconds: int | None = Field(
        default=None,
        ge=10,
        le=2_592_000,
    )

    @field_validator("source_stream_ids")
    @classmethod
    def streams_must_be_unique_and_sorted(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("coverage source stream 必须唯一且排序")
        if any(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", item) is None for item in values):
            raise ValueError("coverage source stream id 非法")
        return values

    @field_validator("required_capabilities")
    @classmethod
    def capabilities_must_be_unique_and_sorted(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("coverage required capability 必须唯一且排序")
        if any(re.fullmatch(r"[A-Z0-9][A-Z0-9_]{0,127}", item) is None for item in values):
            raise ValueError("coverage required capability id 非法")
        return values

    @field_validator("source_capabilities")
    @classmethod
    def source_capabilities_must_be_well_formed(
        cls, values: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for stream, capabilities in values.items():
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", stream) is None:
                raise ValueError("coverage source capability stream id 非法")
            if tuple(sorted(set(capabilities))) != capabilities:
                raise ValueError("coverage source capability 必须唯一且排序")
            if any(
                re.fullmatch(r"[A-Z0-9][A-Z0-9_]{0,127}", item) is None
                for item in capabilities
            ):
                raise ValueError("coverage source capability id 非法")
        return values

    @model_validator(mode="after")
    def capability_contract_must_match_streams(self):
        if (
            self.required_capabilities or self.source_capabilities
        ) and set(self.source_capabilities) != set(self.source_stream_ids):
            raise ValueError("coverage source capability 必须逐一对应配置的数据流")
        provided = {
            capability
            for capabilities in self.source_capabilities.values()
            for capability in capabilities
        }
        unknown = tuple(sorted(provided - set(self.required_capabilities)))
        if unknown:
            raise ValueError(
                "coverage source capability 不属于领域需求: " + ", ".join(unknown)
            )
        return self


class InformationPolicy(StrictConfig):
    version: str
    normalizer_version: str
    trendradar_mcp_url: str = "http://127.0.0.1:3333/mcp"
    newsnow_base_url: str = "http://127.0.0.1:4444"
    newsnow_sources: tuple[str, ...] = ()
    source_timezone: str = "Asia/Shanghai"
    platforms: tuple[str, ...] = ()
    read_limit: int = Field(default=100, ge=1, le=1000)
    request_timeout_seconds: int = Field(default=15, ge=1, le=60)
    collection_interval_seconds: int = Field(default=60, ge=10, le=600)
    fed_monetary_poll_seconds: int = Field(default=15, ge=10, le=300)
    fed_calendar_poll_seconds: int = Field(default=21_600, ge=300, le=86_400)
    treasury_buyback_poll_seconds: int = Field(default=21_600, ge=300, le=86_400)
    treasury_buyback_result_lookback_seconds: int = Field(
        default=604_800,
        ge=86_400,
        le=2_592_000,
    )
    official_metric_poll_seconds: int = Field(default=300, ge=60, le=3_600)
    official_metric_slow_poll_seconds: int = Field(
        default=900,
        ge=300,
        le=86_400,
    )
    coverage_requirements: tuple[CoverageRequirement, ...] = ()

    @model_validator(mode="after")
    def official_metric_cadence_must_be_ordered(self):
        if self.official_metric_slow_poll_seconds < self.official_metric_poll_seconds:
            raise ValueError("官方指标慢速轮询周期不能短于快速周期")
        return self

    @model_validator(mode="after")
    def coverage_domains_must_be_complete_and_unique(self):
        domains = tuple(item.domain for item in self.coverage_requirements)
        if domains and set(domains) != set(CausalDomain):
            raise ValueError("information coverage 必须完整声明全部因果领域")
        if len(set(domains)) != len(domains):
            raise ValueError("information coverage 因果领域不得重复")
        return self

    @field_validator("trendradar_mcp_url")
    @classmethod
    def mcp_must_be_loopback(cls, value: str) -> str:
        if not value.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("信息 MCP 必须是显式回环地址")
        return value

    @field_validator("newsnow_base_url")
    @classmethod
    def newsnow_must_be_loopback(cls, value: str) -> str:
        if not value.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("NewsNow 必须是显式回环地址")
        return value.rstrip("/")

    @field_validator("newsnow_sources")
    @classmethod
    def newsnow_sources_must_be_unique_and_safe(
        cls, sources: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(sources) != len(set(sources)):
            raise ValueError("NewsNow source id 不得重复")
        if any(
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item) is None
            for item in sources
        ):
            raise ValueError("NewsNow source id 非法")
        return sources
