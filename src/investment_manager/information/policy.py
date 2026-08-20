import re

from pydantic import Field, field_validator

from investment_manager.kernel.configuration import StrictConfig


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
