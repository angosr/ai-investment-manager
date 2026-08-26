from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.models import EdgeCalibration
from investment_manager.kernel.configuration import StrictConfig
from investment_manager.state.decision.packet import AnalysisMandate


class StrategyPolicy(StrictConfig):
    strategy_id: str = "price-trend"
    version: str
    family: str = "price-trend"
    enabled: bool = True
    score_threshold: Decimal = Decimal("0.55")
    stop_atr_multiple: Decimal = Decimal("1.5")
    horizon_minutes: int = Field(default=60, gt=0)
    expected_edge_half_life_seconds: int = Field(default=900, ge=1, le=86400)


class CalibrationPolicy(StrictConfig):
    version: str
    minimum_non_overlapping_samples: int = Field(default=30, ge=2)
    method_version: str = "mean-lower-bound-v1"
    lower_confidence_z: Decimal = Field(default=Decimal("1.96"), gt=0)
    artifacts: tuple[EdgeCalibration, ...] = ()

    @model_validator(mode="after")
    def artifacts_must_be_unique_sufficient_and_non_overlapping(self):
        ids = [item.calibration_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("EdgeCalibration calibration_id 必须唯一")
        by_scope: dict[tuple[object, ...], list[EdgeCalibration]] = {}
        for artifact in self.artifacts:
            if artifact.non_overlapping_sample_size < self.minimum_non_overlapping_samples:
                raise ValueError("发布校准制品的非重叠样本量不足")
            if artifact.method_version != self.method_version:
                raise ValueError("EdgeCalibration 构建方法版本与策略不一致")
            by_scope.setdefault(artifact.scope, []).append(artifact)
        for scoped in by_scope.values():
            ordered = sorted(scoped, key=lambda item: item.valid_from)
            if any(
                current.valid_from < previous.valid_until for previous, current in pairwise(ordered)
            ):
                raise ValueError("同一校准作用域的有效期不得重叠")
        return self


class AiMode(StrEnum):
    OFF = "OFF"
    PROPOSE = "PROPOSE"


class PipelinePolicy(StrictConfig):
    version: str


class ResearchPipelinePolicy(PipelinePolicy):
    """Offline legacy replay mode; never part of a managed release config."""

    ai_mode: AiMode = AiMode.OFF


class ProposalPolicy(StrictConfig):
    version: str
    producer_id: str = "codex-analyst"
    strategy_family: str = "ai-contextual"
    minimum_confidence: Decimal = Decimal("0.55")
    maximum_horizon_minutes: int = Field(default=240, gt=0)
    forecast_horizons_minutes: tuple[int, ...] = Field(
        default=(60, 240), min_length=1, max_length=4
    )
    expected_edge_half_life_seconds: int = Field(default=1800, ge=1, le=86400)

    @model_validator(mode="after")
    def forecast_horizons_are_unique_ordered_and_bounded(self):
        horizons = self.forecast_horizons_minutes
        if tuple(sorted(set(horizons))) != horizons:
            raise ValueError("方向预测周期必须正数、唯一且升序")
        if horizons[0] <= 0 or horizons[-1] > self.maximum_horizon_minutes:
            raise ValueError("方向预测周期必须在 Proposal 最大周期内")
        return self


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
