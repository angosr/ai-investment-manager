from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.configuration import StrictConfig
from investment_manager.state.decision.packet import AnalysisMandate


class PipelinePolicy(StrictConfig):
    version: str


class CodexAccount(StrictConfig):
    account_id: str = Field(pattern=r"^\.?[a-z][a-z0-9._-]{1,63}$")
    codex_home: Path
    enabled: bool = False
    capacity_weight: Decimal = Field(default=Decimal("1"), gt=0)

    @field_validator("codex_home")
    @classmethod
    def codex_home_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("codex_home 必须是绝对路径")
        return value

    @model_validator(mode="after")
    def account_id_must_match_directory_name(self):
        if self.account_id != self.codex_home.name:
            raise ValueError("Codex account_id 必须与 codex_home 目录名一致")
        return self


class CodexAccountRegistry(StrictConfig):
    version: str
    accounts: tuple[CodexAccount, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def aliases_and_paths_must_be_unique(self):
        aliases = [item.account_id for item in self.accounts]
        paths = [item.codex_home for item in self.accounts]
        if len(set(aliases)) != len(aliases):
            raise ValueError("Codex account_id 必须唯一")
        if len(set(paths)) != len(paths):
            raise ValueError("Codex codex_home 必须唯一")
        return self


class CodexRuntimePolicy(StrictConfig):
    version: str
    enabled: bool = False
    isolation_verified: bool = False
    binary: Path = Path("/usr/bin/codex")
    bundle_root: Path = Path("/var/lib/investment-manager/codex-runs")
    expected_cli_version: str
    expected_binary_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    model: str
    reasoning_effort: str = Field(pattern=r"^(low|medium|high|xhigh|max|ultra)$")
    maximum_prompt_characters: int = Field(default=16_000, ge=8_000, le=16_000)
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    capacity_probe_timeout_seconds: int = Field(default=10, ge=1, le=60)
    capacity_ttl_seconds: int = Field(default=60, ge=1, le=60)
    transient_failure_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    lease_ttl_seconds: int = Field(default=240, ge=30, le=1200)
    max_account_switches: int = Field(default=2, ge=0, le=2)

    @field_validator("binary")
    @classmethod
    def binary_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Codex binary 必须是绝对路径")
        return value

    @field_validator("bundle_root")
    @classmethod
    def bundle_root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Codex bundle_root 必须是绝对路径")
        return value

    @model_validator(mode="after")
    def production_requires_verified_isolation(self):
        if self.enabled and not self.isolation_verified:
            raise ValueError("启用真实 Codex 前必须完成 OS/Profile 隔离验证")
        if self.enabled and self.expected_binary_sha256 is None:
            raise ValueError("启用真实 Codex 前必须冻结可执行文件 SHA-256")
        return self


class ContextAssessmentPolicy(StrictConfig):
    version: str
    enabled: bool = False
    review_trigger_symbol: str | None = Field(
        default=None,
        pattern=r"^[A-Z0-9]+$",
        exclude_if=lambda value: value is None,
    )
    mandate: AnalysisMandate
