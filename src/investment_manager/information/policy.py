import re
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import CausalDomain
from investment_manager.kernel.configuration import StrictConfig


class OfficialEventFeed(StrictConfig):
    """Pinned first-party release feed; discovery and relevance remain separate."""

    stream_id: str
    url: str
    entry_path_pattern: str | None = None

    @field_validator("stream_id")
    @classmethod
    def stream_id_must_be_safe(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value) is None:
            raise ValueError("official event feed stream id 非法")
        return value

    @field_validator("url")
    @classmethod
    def url_must_be_pinned_government_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or not parsed.hostname.endswith(".gov")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("official event feed 必须是无凭据、无查询的固定 .gov HTTPS URL")
        return value

    @field_validator("entry_path_pattern")
    @classmethod
    def entry_path_pattern_must_be_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 200 or not value.startswith("^") or not value.endswith("$"):
            raise ValueError("official event entry pattern 必须完整锚定且不超过 200 字符")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("official event entry pattern 非法") from exc
        return value


class OfficialPublicationFeed(StrictConfig):
    """Pinned first-party HTML publication index with a bounded entry route."""

    stream_id: str
    index_url: str
    entry_path_pattern: str
    domain: CausalDomain
    maximum_entries: int = Field(default=10, ge=1, le=25)

    @field_validator("stream_id")
    @classmethod
    def stream_id_must_be_safe(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value) is None:
            raise ValueError("official publication stream id 非法")
        return value

    @field_validator("index_url")
    @classmethod
    def index_url_must_be_pinned_government_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or not parsed.hostname.endswith(".gov")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("official publication index 必须是无凭据、无查询的固定 .gov HTTPS URL")
        return value.rstrip("/")

    @field_validator("entry_path_pattern")
    @classmethod
    def entry_path_pattern_must_be_bounded(cls, value: str) -> str:
        if len(value) > 200 or not value.startswith("^") or not value.endswith("$"):
            raise ValueError("official publication entry pattern 必须完整锚定且不超过 200 字符")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("official publication entry pattern 非法") from exc
        return value


class CoverageSourceContract(StrictConfig):
    stream_id: str
    capabilities: tuple[str, ...] = ()
    maximum_publication_age_seconds: int | None = Field(
        default=None,
        ge=10,
        le=2_592_000,
    )

    @field_validator("stream_id")
    @classmethod
    def stream_id_must_be_safe(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value) is None:
            raise ValueError("coverage source stream id 非法")
        return value

    @field_validator("capabilities")
    @classmethod
    def capabilities_must_be_unique_and_sorted(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("coverage source capability 必须唯一且排序")
        if any(re.fullmatch(r"[A-Z0-9][A-Z0-9_]{0,127}", item) is None for item in values):
            raise ValueError("coverage source capability id 非法")
        return values


class CoverageRequirement(StrictConfig):
    domain: CausalDomain
    sources: tuple[CoverageSourceContract, ...] = ()
    required_capabilities: tuple[str, ...] = ()

    @field_validator("sources")
    @classmethod
    def sources_must_be_unique_and_sorted(
        cls, values: tuple[CoverageSourceContract, ...]
    ) -> tuple[CoverageSourceContract, ...]:
        identities = tuple(item.stream_id for item in values)
        if tuple(sorted(set(identities))) != identities:
            raise ValueError("coverage source contract 必须按 stream_id 唯一排序")
        return values

    @field_validator("required_capabilities")
    @classmethod
    def required_capabilities_must_be_unique_and_sorted(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("coverage required capability 必须唯一且排序")
        if any(
            re.fullmatch(r"[A-Z0-9][A-Z0-9_]{0,127}", item) is None
            for item in values
        ):
            raise ValueError("coverage required capability id 非法")
        return values

    @model_validator(mode="after")
    def capability_contract_must_match_domain(self):
        provided = {
            capability
            for source in self.sources
            for capability in source.capabilities
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
    official_event_feeds: tuple[OfficialEventFeed, ...] = ()
    official_publication_feeds: tuple[OfficialPublicationFeed, ...] = ()
    fed_monetary_poll_seconds: int = Field(default=15, ge=10, le=300)
    fed_calendar_poll_seconds: int = Field(default=21_600, ge=300, le=86_400)
    economic_release_calendar_poll_seconds: int = Field(
        default=21_600,
        ge=300,
        le=86_400,
    )
    economic_release_actual_poll_seconds: int = Field(default=15, ge=5, le=300)
    economic_release_actual_deadline_seconds: int = Field(
        default=900,
        ge=60,
        le=7_200,
    )
    economic_release_actual_recovery_lookback_seconds: int = Field(
        default=7_200,
        ge=900,
        le=86_400,
    )
    treasury_buyback_poll_seconds: int = Field(default=21_600, ge=300, le=86_400)
    treasury_buyback_result_lookback_seconds: int = Field(
        default=604_800,
        ge=86_400,
        le=2_592_000,
    )
    regulatory_poll_seconds: int = Field(default=300, ge=60, le=3_600)
    official_metric_poll_seconds: int = Field(default=300, ge=60, le=3_600)
    etf_aggregate_flow_poll_seconds: int = Field(default=300, ge=60, le=3_600)
    official_metric_slow_poll_seconds: int = Field(
        default=900,
        ge=300,
        le=86_400,
    )
    coverage_requirements: tuple[CoverageRequirement, ...] = ()

    @field_validator("official_event_feeds")
    @classmethod
    def official_event_feeds_must_be_unique_and_sorted(
        cls, feeds: tuple[OfficialEventFeed, ...]
    ) -> tuple[OfficialEventFeed, ...]:
        identities = tuple(item.stream_id for item in feeds)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("official event feeds 必须按 stream_id 唯一且排序")
        return feeds

    @field_validator("official_publication_feeds")
    @classmethod
    def official_publication_feeds_must_be_unique_and_sorted(
        cls, feeds: tuple[OfficialPublicationFeed, ...]
    ) -> tuple[OfficialPublicationFeed, ...]:
        identities = tuple(item.stream_id for item in feeds)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("official publication feeds 必须按 stream_id 唯一且排序")
        return feeds

    @model_validator(mode="after")
    def official_metric_cadence_must_be_ordered(self):
        if self.official_metric_slow_poll_seconds < self.official_metric_poll_seconds:
            raise ValueError("官方指标慢速轮询周期不能短于快速周期")
        if (
            self.economic_release_actual_deadline_seconds
            < self.economic_release_actual_poll_seconds
            or self.economic_release_actual_recovery_lookback_seconds
            < self.economic_release_actual_deadline_seconds
        ):
            raise ValueError("经济发布实际值截止与恢复窗口必须覆盖轮询周期")
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
    def newsnow_sources_must_be_unique_and_safe(cls, sources: tuple[str, ...]) -> tuple[str, ...]:
        if len(sources) != len(set(sources)):
            raise ValueError("NewsNow source id 不得重复")
        if any(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item) is None for item in sources):
            raise ValueError("NewsNow source id 非法")
        return sources
