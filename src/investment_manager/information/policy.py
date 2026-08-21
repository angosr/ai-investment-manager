import re

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import CausalDomain
from investment_manager.kernel.configuration import StrictConfig


class CoverageRequirement(StrictConfig):
    domain: CausalDomain
    source_stream_ids: tuple[str, ...] = ()
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
    coverage_requirements: tuple[CoverageRequirement, ...] = ()

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
