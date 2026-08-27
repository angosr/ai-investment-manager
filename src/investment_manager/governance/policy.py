from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.configuration import StrictConfig
from investment_manager.kernel.time import require_utc


class WorldModelAblationPolicy(StrictConfig):
    """Prospective paired control; it can never produce capital instructions."""

    version: str
    enabled: bool = False
    plan_id: str = Field(min_length=1)
    activated_at: datetime
    minimum_sample_size: int = Field(default=30, ge=2)

    _utc_activated_at = field_validator("activated_at")(require_utc)


class ContextForecastStabilityPolicy(StrictConfig):
    """Prospective exact-input replicas with no Forecast or capital authority."""

    version: str = Field(min_length=1)
    enabled: bool = False
    activated_at: datetime
    replicas_per_input: int = Field(default=1, ge=1, le=3)

    _utc_activated_at = field_validator("activated_at")(require_utc)


class QuantBaselineArtifactPolicy(StrictConfig):
    """One immutable program Forecast artifact bound to an economic family."""

    outcome_family_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def path_is_repository_relative(self):
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Quant baseline 制品必须使用仓库内相对路径")
        return self


class QuantBaselinePolicy(StrictConfig):
    """Research-only deterministic Forecast producer; never a capital source."""

    version: str = Field(min_length=1)
    enabled: bool = False
    producer_id: str = Field(min_length=1)
    artifacts: tuple[QuantBaselineArtifactPolicy, ...] = ()

    @model_validator(mode="after")
    def artifacts_are_unique_and_sorted(self):
        families = tuple(item.outcome_family_id for item in self.artifacts)
        if families != tuple(sorted(set(families))):
            raise ValueError("Quant baseline 制品必须按唯一 outcome family 排序")
        ids = tuple(item.artifact_id for item in self.artifacts)
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ValueError("Quant baseline 制品 ID 与路径不得重复")
        return self


class QuantContextPosteriorPolicy(StrictConfig):
    """Research-only AI posterior over one frozen deterministic Quant prior."""

    version: str = Field(min_length=1)
    enabled: bool = False
    producer_id: str = Field(min_length=1)
    activated_at: datetime
    assignment_poll_seconds: int = Field(default=5, ge=1, le=60)

    _utc_activated_at = field_validator("activated_at")(require_utc)


class OutcomeEvaluationPolicy(StrictConfig):
    version: str
    forecast_version: str = "analysis-forecast-v3"
    target_forecast_version: str = "forecast-target-outcome-v1"
    target_forecast_minimum_sample_size: int = Field(default=30, ge=1)
    product_payoff_version: str = "product-payoff-outcome-v1"
    product_payoff_minimum_sample_size: int = Field(default=30, ge=1)
    maximum_funding_gap_hours: int = Field(default=12, ge=1, le=24)
    window_hours: int = Field(default=24, ge=1, le=168)
    settlement_grace_minutes: int = Field(default=120, ge=0, le=1440)
    poll_seconds: int = Field(default=300, ge=10, le=3600)
    world_model_ablation: WorldModelAblationPolicy | None = None
    context_forecast_stability: ContextForecastStabilityPolicy | None = None
    quant_baseline: QuantBaselinePolicy | None = None
    quant_context_posterior: QuantContextPosteriorPolicy | None = None


class GovernancePolicy(StrictConfig):
    version: str
    snapshot_lookback_days: int = Field(default=90, ge=7, le=3650)
    maximum_metric_windows: int = Field(default=30, ge=1, le=200)
    maximum_failed_experiments: int = Field(default=20, ge=1, le=100)
    maximum_open_proposals: int = Field(default=20, ge=1, le=100)
    maximum_evaluation_plans: int = Field(default=10, ge=1, le=50)
    complexity_limit: int = Field(default=10, ge=0, le=100)
    cycle_interval_hours: int = Field(default=24, ge=1, le=168)


class DeploymentStage(StrEnum):
    MOCK = "MOCK"
    SHADOW = "SHADOW"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class DeploymentPolicy(StrictConfig):
    version: str
    stage: DeploymentStage = DeploymentStage.MOCK
    shadow_market_data_enabled: bool = False
    testnet_order_submission_enabled: bool = False
    live_order_submission_enabled: bool = False
    credential_profile: str | None = None
    manual_approval_ref: str | None = None

    @model_validator(mode="after")
    def stage_permissions_are_fail_closed(self):
        if self.stage == DeploymentStage.MOCK and any(
            (
                self.shadow_market_data_enabled,
                self.testnet_order_submission_enabled,
                self.live_order_submission_enabled,
                self.credential_profile is not None,
            )
        ):
            raise ValueError("MOCK 阶段不得启用外部数据凭据或订单权限")
        if self.stage == DeploymentStage.SHADOW and (
            not self.shadow_market_data_enabled
            or self.testnet_order_submission_enabled
            or self.live_order_submission_enabled
        ):
            raise ValueError("SHADOW 只允许实时只读行情，不允许任何订单提交")
        if self.stage == DeploymentStage.TESTNET and (
            not self.shadow_market_data_enabled
            or not self.testnet_order_submission_enabled
            or self.live_order_submission_enabled
            or self.credential_profile is None
            or self.manual_approval_ref is None
        ):
            raise ValueError("TESTNET 必须显式启用测试网、凭据 Profile 和人工批准")
        if self.stage == DeploymentStage.LIVE:
            raise ValueError("LIVE 适配器未实现，配置层禁止启用")
        return self
