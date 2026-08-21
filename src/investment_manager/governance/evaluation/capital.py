"""Preregistered evaluation contract for one exact Capital Shadow release."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.governance.models import (
    EvaluationPlan,
    EvaluationStage,
    ReleaseArtifact,
    ReleaseManifest,
    validate_manifest_against_config,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.settings import AppConfig


class CapitalShadowThresholds(FrozenModel):
    calendar_months: int = Field(ge=12)
    minimum_forecast_available_months: int = Field(gt=0)
    minimum_decision_complete_months: int = Field(gt=0)
    minimum_positive_months: int = Field(gt=0)
    maximum_late_entries: Literal[0] = 0
    maximum_duplicate_execution_groups: Literal[0] = 0
    maximum_unhedged_seconds: int = Field(gt=0)
    maximum_group_recovery_seconds: int = Field(gt=0)
    minimum_annualized_return_lower_bound: Decimal = Decimal("0")
    maximum_source_policy_underperformance_fraction: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=1,
    )
    lower_confidence_z: Decimal = Field(default=Decimal("2.201"), gt=0)
    newey_west_lag_months: Literal[3] = 3
    maximum_drawdown_fraction: Decimal = Field(gt=0, le=1)
    minimum_margin_buffer_fraction: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def month_thresholds_fit_window(self):
        if not (
            self.minimum_positive_months
            <= self.minimum_forecast_available_months
            <= self.minimum_decision_complete_months
            == self.calendar_months
        ):
            raise ValueError("Capital Shadow 月度门槛与观察窗口不一致")
        return self


class CapitalShadowEvaluationSpec(FrozenModel):
    """The immutable cohort, baselines, metrics, and failure rules for Shadow."""

    version: Literal["capital-shadow-evaluation-spec-v1"] = (
        "capital-shadow-evaluation-spec-v1"
    )
    plan_id: str
    release_manifest_id: str
    release_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    release_configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_component_versions: tuple[tuple[str, str], ...] = Field(min_length=1)
    release_artifacts: tuple[ReleaseArtifact, ...] = Field(min_length=1)
    capital_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_id: str
    source_evaluation_id: str
    source_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_policy_version: str
    observation_start: datetime
    observation_end: datetime
    settlement_grace_days: int = Field(default=7, ge=1, le=31)
    starting_equity: Decimal = Field(gt=0)
    baselines: tuple[str, ...] = (
        "CASH",
        "SOURCE_RESEARCH_POLICY_COUNTERFACTUAL",
    )
    behavior_contract: tuple[str, ...] = (
        "MONTHLY_FIRST_OPEN_ONLY",
        "NO_LATE_ENTRY",
        "FROZEN_BASE_QUANTITY_WITHIN_MONTH",
        "RECOVER_OR_COMPENSATE_NONTERMINAL_GROUP",
        "PROGRAMMATIC_RISK_EXIT_ONLY",
        "NO_TARGET_RETRACKING_WITHIN_MONTH",
    )
    accounting_dimensions: tuple[str, ...] = (
        "NET_EQUITY",
        "CAPITAL_UTILIZATION",
        "FEE",
        "SPREAD",
        "FUNDING",
        "BASIS",
        "COMPENSATION_LOSS",
    )
    cohort_rule: Literal["INVALIDATE_ON_ANY_BOUND_IDENTITY_CHANGE"] = (
        "INVALIDATE_ON_ANY_BOUND_IDENTITY_CHANGE"
    )
    thresholds: CapitalShadowThresholds

    _utc_observation_start = field_validator("observation_start")(require_utc)
    _utc_observation_end = field_validator("observation_end")(require_utc)

    @model_validator(mode="after")
    def identities_and_window_are_exact(self):
        if len(dict(self.release_component_versions)) != len(
            self.release_component_versions
        ):
            raise ValueError("Capital Shadow Release 组件版本不得重复")
        months = _calendar_month_count(self.observation_start, self.observation_end)
        if months != self.thresholds.calendar_months:
            raise ValueError("Capital Shadow 观察月份与门槛不一致")
        if tuple(sorted(set(self.baselines))) != self.baselines:
            raise ValueError("Capital Shadow 基线必须唯一且有序")
        if len(set(self.behavior_contract)) != len(self.behavior_contract):
            raise ValueError("Capital Shadow 行为合同不得重复")
        if len(set(self.accounting_dimensions)) != len(self.accounting_dimensions):
            raise ValueError("Capital Shadow 归因维度不得重复")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        plan_id: str,
        config: AppConfig,
        manifest: ReleaseManifest,
        observation_start: datetime,
        observation_end: datetime,
    ) -> CapitalShadowEvaluationSpec:
        validate_manifest_against_config(
            manifest,
            config,
            require_configuration_hash=True,
        )
        if not config.capital.enabled or config.deployment.stage != DeploymentStage.SHADOW:
            raise ValueError("Capital 评价只能冻结已启用的 SHADOW Release")
        evidence = config.carry_forecast.evidence
        if evidence is None or manifest.configuration_hash is None:
            raise ValueError("Capital 评价必须绑定完整 Release 与 Carry evidence")
        months = _calendar_month_count(observation_start, observation_end)
        behavior_payload = {
            "carry_forecast": config.carry_forecast.model_dump(mode="json"),
            "capital": config.capital.model_dump(mode="json"),
            "market_data": config.market_data.model_dump(mode="json"),
            "trigger": config.trigger.model_dump(mode="json"),
            "temporal": config.temporal.model_dump(mode="json"),
            "shadow": config.shadow.model_dump(mode="json"),
        }
        return cls(
            plan_id=plan_id,
            release_manifest_id=manifest.manifest_id,
            release_code_version=manifest.code_version,
            release_configuration_hash=manifest.configuration_hash,
            release_component_versions=manifest.component_versions,
            release_artifacts=manifest.artifacts,
            capital_behavior_hash=content_hash(behavior_payload),
            portfolio_id=config.capital.decision.portfolio_id,
            source_evaluation_id=evidence.source_evaluation_id,
            source_result_hash=evidence.source_result_hash,
            source_policy_version=evidence.evaluated_policy_version,
            observation_start=observation_start,
            observation_end=observation_end,
            starting_equity=config.shadow.initial_quote_balance,
            thresholds=CapitalShadowThresholds(
                calendar_months=months,
                minimum_forecast_available_months=months - 1,
                minimum_decision_complete_months=months,
                minimum_positive_months=(months * 3 + 3) // 4,
                maximum_unhedged_seconds=(
                    config.capital.risk.maximum_unhedged_seconds
                ),
                maximum_group_recovery_seconds=(
                    config.trigger.heartbeat_minutes * 60
                    + config.temporal.activity_schedule_to_close_seconds
                ),
                maximum_drawdown_fraction=(
                    config.capital.risk.maximum_drawdown_fraction
                ),
                minimum_margin_buffer_fraction=Decimal("0.05"),
            ),
        )


def build_capital_shadow_evaluation_plan(
    *,
    spec: CapitalShadowEvaluationSpec,
    registered_at: datetime,
) -> EvaluationPlan:
    registered_at = require_utc(registered_at)
    if registered_at >= spec.observation_start:
        raise ValueError("Capital Shadow 计划必须在首个观察月开始前登记")
    return EvaluationPlan(
        plan_id=spec.plan_id,
        registered_at=registered_at,
        base_manifest_id=spec.release_manifest_id,
        primary_metric="annualized_net_equity_return_lower_bound_vs_cash",
        minimum_sample_size=spec.thresholds.calendar_months,
        hard_guardrails=(
            "BOUND_RELEASE_AND_EVIDENCE_UNCHANGED",
            "MONTHLY_DECISIONS_COMPLETE",
            "NO_LATE_ENTRY",
            "NO_DUPLICATE_EXECUTION_GROUP",
            "UNHEDGED_DURATION_WITHIN_LIMIT",
            "GROUP_RECOVERY_WITHIN_LIMIT",
            "NET_EQUITY_RECONCILES_AFTER_ALL_COSTS",
            "ANNUALIZED_RETURN_LOWER_BOUND_POSITIVE_VS_CASH",
            "SOURCE_POLICY_ANNUAL_RETURN_GAP_WITHIN_LIMIT",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
            "MARGIN_BUFFER_WITHIN_LIMIT",
        ),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.SHADOW,
        ),
        fixed_regression_suite_version="investment-manager-capital-shadow-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=0,
    )


def validate_capital_shadow_evaluation_plan(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    plans: tuple[EvaluationPlan, ...],
    started_at: datetime,
) -> tuple[CapitalShadowEvaluationSpec, EvaluationPlan]:
    """Require one exact preregistered evaluation contract before Capital starts."""

    started_at = require_utc(started_at)
    candidates: list[tuple[CapitalShadowEvaluationSpec, EvaluationPlan]] = []
    for plan in plans:
        snapshot = plan.candidate_spec_snapshot
        if not isinstance(snapshot, dict) or snapshot.get("version") != (
            "capital-shadow-evaluation-spec-v1"
        ):
            continue
        try:
            spec = CapitalShadowEvaluationSpec.model_validate(snapshot)
            expected_spec = CapitalShadowEvaluationSpec.freeze(
                plan_id=spec.plan_id,
                config=config,
                manifest=manifest,
                observation_start=spec.observation_start,
                observation_end=spec.observation_end,
            )
            expected_plan = build_capital_shadow_evaluation_plan(
                spec=expected_spec,
                registered_at=plan.registered_at,
            )
        except ValueError as exc:
            raise ValueError("Capital EvaluationPlan 不是有效的完整预登记合同") from exc
        if spec != expected_spec or plan != expected_plan:
            raise ValueError("Capital EvaluationPlan 与当前 Release 完整合同不一致")
        if plan.registered_at > started_at:
            raise ValueError("Capital EvaluationPlan 登记时间晚于本次服务启动时间")
        candidates.append((spec, plan))
    if len(candidates) != 1:
        raise ValueError("Capital Release 必须恰好绑定一个精确的预登记评价合同")
    return candidates[0]


def _calendar_month_count(start: datetime, end: datetime) -> int:
    start = require_utc(start)
    end = require_utc(end)
    if any(
        (
            start.day != 1,
            end.day != 1,
            start.time().replace(tzinfo=None) != datetime.min.time(),
            end.time().replace(tzinfo=None) != datetime.min.time(),
        )
    ):
        raise ValueError("Capital Shadow 窗口必须由 UTC 自然月边界组成")
    months = (end.year - start.year) * 12 + end.month - start.month
    if months < 12:
        raise ValueError("Capital Shadow 正式评价至少需要十二个完整自然月")
    return months
