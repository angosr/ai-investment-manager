"""Preregistered evaluation contract for one exact Capital Shadow release."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.governance.evaluation.statistics import (
    conservative_newey_west_lower_bound,
)
from investment_manager.governance.models import (
    EvaluationPlan,
    EvaluationStage,
    ReleaseArtifact,
    ReleaseManifest,
    validate_manifest_against_config,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.settings import AppConfig

_MONTHLY_COMPOUNDING_RELATIVE_TOLERANCE = Decimal("1e-24")


def equity_values_reconcile(calculated: Decimal, authoritative: Decimal) -> bool:
    """Allow only deterministic Decimal division noise in monthly compounding."""

    scale = max(abs(calculated), abs(authoritative), Decimal("1"))
    return abs(calculated - authoritative) <= (scale * _MONTHLY_COMPOUNDING_RELATIVE_TOLERANCE)


def capital_behavior_hash(config: AppConfig) -> str:
    """Identity of every setting that can change the Capital evaluation cohort."""

    return content_hash(
        {
            "carry_forecast": config.carry_forecast.model_dump(mode="json"),
            "capital": config.capital.model_dump(mode="json"),
            "market_data": config.market_data.model_dump(mode="json"),
            "trigger": config.trigger.model_dump(mode="json"),
            "temporal": config.temporal.model_dump(mode="json"),
            "shadow": config.shadow.model_dump(mode="json"),
        }
    )


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
    maximum_account_boundary_delay_seconds: int | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
    )

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

    version: Literal[
        "capital-shadow-evaluation-spec-v1",
        "capital-shadow-evaluation-spec-v2",
        "capital-shadow-evaluation-spec-v3",
        "capital-shadow-evaluation-spec-v4",
    ] = "capital-shadow-evaluation-spec-v4"
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
    equity_boundary_rule: Literal["EARLIEST_AUTHORITATIVE_REVISION_AT_OR_AFTER_BOUNDARY"] | None = (
        Field(
            default=None,
            exclude_if=lambda value: value is None,
        )
    )
    baselines: tuple[str, ...] = (
        "CASH",
        "SOURCE_RESEARCH_POLICY_COUNTERFACTUAL",
    )
    behavior_contract: tuple[str, ...] = (
        "SAME_DECISION_CHAIN_ABOVE_VENUE",
        "NATURAL_SIGNAL_NO_FORCED_TRADES",
        "MOCK_HYPOTHESIS_EDGE_EXPLICITLY_LABELED",
        "ONE_ACTIVE_CANDIDATE_PRODUCER",
        "MONTHLY_COUNTERFACTUAL_DOES_NOT_CONSUME_CAPITAL",
        "RECOVER_OR_COMPENSATE_NONTERMINAL_GROUP",
        "PROGRAMMATIC_RISK_EXIT_ONLY",
        "NO_REAL_ORDER_PERMISSION",
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
        if len(dict(self.release_component_versions)) != len(self.release_component_versions):
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
        bounded_boundaries = self.version == "capital-shadow-evaluation-spec-v4"
        if bounded_boundaries != (self.equity_boundary_rule is not None):
            raise ValueError("Capital Shadow 账户月界规则与评价版本不一致")
        if bounded_boundaries != (
            self.thresholds.maximum_account_boundary_delay_seconds is not None
        ):
            raise ValueError("Capital Shadow 账户月界最大延迟与评价版本不一致")
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
        permissions = config.capital.mock_candidate_authorizations
        if len(permissions) != 1:
            raise ValueError("Capital 评价必须绑定唯一主动候选权限")
        if any(item.evaluation_plan_id != plan_id for item in permissions):
            raise ValueError("Mock candidate authorization 必须绑定本 Capital EvaluationPlan")
        return cls(
            plan_id=plan_id,
            release_manifest_id=manifest.manifest_id,
            release_code_version=manifest.code_version,
            release_configuration_hash=manifest.configuration_hash,
            release_component_versions=manifest.component_versions,
            release_artifacts=manifest.artifacts,
            capital_behavior_hash=capital_behavior_hash(config),
            portfolio_id=config.capital.decision.portfolio_id,
            source_evaluation_id=evidence.source_evaluation_id,
            source_result_hash=evidence.source_result_hash,
            source_policy_version=evidence.evaluated_policy_version,
            observation_start=observation_start,
            observation_end=observation_end,
            starting_equity=config.shadow.initial_quote_balance,
            equity_boundary_rule=("EARLIEST_AUTHORITATIVE_REVISION_AT_OR_AFTER_BOUNDARY"),
            behavior_contract=(
                *cls.model_fields["behavior_contract"].default,
                "BOUNDED_AUTHORITATIVE_ACCOUNT_BOUNDARIES",
                "OBSERVATION_RETURN_STARTS_FROM_BOUNDARY_ACCOUNT_EQUITY",
            ),
            thresholds=CapitalShadowThresholds(
                calendar_months=months,
                minimum_forecast_available_months=months - 1,
                minimum_decision_complete_months=months,
                minimum_positive_months=(months * 3 + 3) // 4,
                maximum_unhedged_seconds=(config.capital.risk.maximum_unhedged_seconds),
                maximum_group_recovery_seconds=(
                    config.trigger.heartbeat_minutes * 60
                    + config.temporal.activity_schedule_to_close_seconds
                ),
                maximum_drawdown_fraction=(config.capital.risk.maximum_drawdown_fraction),
                minimum_margin_buffer_fraction=Decimal("0.05"),
                maximum_account_boundary_delay_seconds=(
                    config.trigger.heartbeat_minutes * 60
                    + config.temporal.activity_schedule_to_close_seconds
                ),
            ),
        )


class CapitalShadowEvaluationStatus(StrEnum):
    INCOMPLETE = "INCOMPLETE"
    PASSED = "PASSED"
    FAILED = "FAILED"


class CapitalLedgerProjection(FrozenModel):
    """One immutable projection of the authoritative Capital ledgers."""

    version: Literal["capital-ledger-projection-v1"] = "capital-ledger-projection-v1"
    plan_id: str
    projected_at: datetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    monthly_net_return_fractions: tuple[Decimal, ...]
    forecast_available_months: int = Field(ge=0)
    decision_complete_months: int = Field(ge=0)
    late_entry_count: int = Field(ge=0)
    duplicate_execution_group_count: int = Field(ge=0)
    unresolved_execution_group_count: int = Field(ge=0)
    maximum_unhedged_seconds: int = Field(ge=0)
    maximum_group_recovery_seconds: int = Field(ge=0)
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal = Field(gt=0)
    price_pnl: Decimal
    funding_pnl: Decimal
    fee_cost: Decimal = Field(ge=0)
    execution_slippage_cost: Decimal = Field(ge=0)
    compensation_loss: Decimal = Field(ge=0)
    net_pnl: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0, le=1)
    minimum_margin_buffer_fraction: Decimal = Field(le=1)
    source_counterfactual_annualized_return_fraction: Decimal | None = None

    _utc_projected_at = field_validator("projected_at")(require_utc)

    @classmethod
    def create(cls, **values) -> CapitalLedgerProjection:
        normalized = {
            **values,
            "source_ids": tuple(sorted(set(values["source_ids"]))),
        }
        candidate = cls.model_construct(**normalized, source_hash="0" * 64)
        source_hash = content_hash(candidate.model_dump(exclude={"source_hash"}, mode="json"))
        return cls(
            **normalized,
            source_hash=source_hash,
        )

    @model_validator(mode="after")
    def source_identity_and_accounting_reconcile(self):
        if tuple(sorted(set(self.source_ids))) != self.source_ids:
            raise ValueError("Capital ledger source_ids 必须唯一且排序")
        payload = self.model_dump(exclude={"source_hash"}, mode="json")
        if self.source_hash != content_hash(payload):
            raise ValueError("Capital ledger projection source_hash 不一致")
        if self.net_pnl != self.price_pnl + self.funding_pnl - self.fee_cost:
            raise ValueError("Capital ledger projection 费用后损益无法核对")
        if self.ending_equity - self.starting_equity != self.net_pnl:
            raise ValueError("Capital ledger projection 权益变化与净损益不一致")
        compounded = self.starting_equity
        for monthly_return in self.monthly_net_return_fractions:
            if monthly_return <= Decimal("-1"):
                raise ValueError("Capital ledger 月度收益不得使权益归零或为负")
            compounded *= Decimal("1") + monthly_return
        if not equity_values_reconcile(compounded, self.ending_equity):
            raise ValueError("Capital ledger projection 月度收益与期末权益不一致")
        return self


class CapitalShadowEvaluationMetrics(FrozenModel):
    calendar_months: int = Field(ge=1)
    forecast_available_months: int = Field(ge=0)
    decision_complete_months: int = Field(ge=0)
    positive_months: int = Field(ge=0)
    annualized_net_return_fraction: Decimal
    annualized_net_return_lower_bound: Decimal
    source_counterfactual_annualized_return_fraction: Decimal
    source_policy_underperformance_fraction: Decimal
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal = Field(gt=0)
    net_pnl: Decimal
    price_pnl: Decimal
    funding_pnl: Decimal
    fee_cost: Decimal = Field(ge=0)
    execution_slippage_cost: Decimal = Field(ge=0)
    compensation_loss: Decimal = Field(ge=0)
    maximum_drawdown_fraction: Decimal = Field(ge=0, le=1)
    minimum_margin_buffer_fraction: Decimal = Field(le=1)
    late_entry_count: int = Field(ge=0)
    duplicate_execution_group_count: int = Field(ge=0)
    unresolved_execution_group_count: int = Field(ge=0)
    maximum_unhedged_seconds: int = Field(ge=0)
    maximum_group_recovery_seconds: int = Field(ge=0)


class CapitalShadowEvaluationResult(FrozenModel):
    version: Literal["capital-shadow-evaluation-result-v1"] = "capital-shadow-evaluation-result-v1"
    result_id: str
    plan_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: datetime
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CapitalShadowEvaluationStatus
    metrics: CapitalShadowEvaluationMetrics | None = None
    reason_codes: tuple[str, ...]
    source_ids: tuple[str, ...] = ()

    _utc_published_at = field_validator("published_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_status_match(self):
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("Capital evaluation reason_codes 必须唯一且排序")
        if tuple(sorted(set(self.source_ids))) != self.source_ids:
            raise ValueError("Capital evaluation source_ids 必须唯一且排序")
        if (self.status == CapitalShadowEvaluationStatus.INCOMPLETE) == (self.metrics is not None):
            raise ValueError("Capital evaluation 完整状态与 metrics 不一致")
        expected = stable_id(
            "capital_shadow_evaluation",
            self.plan_id,
            self.evaluation_spec_hash,
            self.published_at,
            self.source_hash,
            self.status,
            self.metrics,
            self.reason_codes,
            self.source_ids,
        )
        if self.result_id != expected:
            raise ValueError("Capital evaluation result_id 不一致")
        return self


class _CapitalShadowEvaluationArtifact(FrozenModel):
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection: CapitalLedgerProjection | None = None
    result: CapitalShadowEvaluationResult

    @model_validator(mode="after")
    def artifact_reconciles(self):
        payload = self.model_dump(exclude={"artifact_hash"}, mode="json")
        if self.artifact_hash != content_hash(payload):
            raise ValueError("Capital evaluation 制品内容哈希不一致")
        if self.projection is not None and (
            self.projection.plan_id != self.result.plan_id
            or self.projection.source_hash != self.result.source_hash
        ):
            raise ValueError("Capital evaluation 结果未绑定同一账本投影")
        if self.result.metrics is not None and self.projection is None:
            raise ValueError("完整 Capital evaluation 结果必须保留账本投影")
        return self


class CapitalShadowEvaluationCatalog:
    """Atomic content-verified artifacts for one preregistered Capital result."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(
        self,
        result: CapitalShadowEvaluationResult,
        *,
        projection: CapitalLedgerProjection | None,
    ) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            existing = self.load(result.result_id)
            if existing.result != result or existing.projection != projection:
                raise ValueError("同一 Capital evaluation 结果 ID 的内容不一致")
            return target
        payload = {
            "projection": (projection.model_dump(mode="json") if projection is not None else None),
            "result": result.model_dump(mode="json"),
        }
        envelope = _CapitalShadowEvaluationArtifact(
            artifact_hash=content_hash(payload),
            projection=projection,
            result=result,
        )
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".capital-shadow-evaluation-",
            payload=envelope,
        )

    def load(self, result_id: str) -> _CapitalShadowEvaluationArtifact:
        raw = json.loads((self._root / f"{result_id}.json").read_text(encoding="utf-8"))
        envelope = _CapitalShadowEvaluationArtifact.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("Capital evaluation 文件名与结果 ID 不一致")
        return envelope


def build_capital_shadow_evaluation_plan(
    *,
    spec: CapitalShadowEvaluationSpec,
    registered_at: datetime,
) -> EvaluationPlan:
    registered_at = require_utc(registered_at)
    if registered_at >= spec.observation_start:
        raise ValueError("Capital Shadow 计划必须在首个观察月开始前登记")
    legacy = spec.version == "capital-shadow-evaluation-spec-v1"
    bounded_boundaries = spec.version == "capital-shadow-evaluation-spec-v4"
    hard_guardrails = (
        (
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
        )
        if legacy
        else (
            "BOUND_RELEASE_AND_FORECAST_PERMISSIONS_UNCHANGED",
            "ALL_ADMITTED_CAPITAL_DECISIONS_COMPLETE",
            "MOCK_HYPOTHESIS_NEVER_USED_AS_CALIBRATION",
            "NO_FORCED_TRADE_OR_SIMULATION_FREQUENCY_GATE",
            "NO_LATE_ENTRY",
            "NO_DUPLICATE_EXECUTION_GROUP",
            "UNHEDGED_DURATION_WITHIN_LIMIT",
            "GROUP_RECOVERY_WITHIN_LIMIT",
            "NET_EQUITY_RECONCILES_AFTER_ALL_COSTS",
            "ANNUALIZED_RETURN_LOWER_BOUND_POSITIVE_VS_CASH",
            "SOURCE_POLICY_ANNUAL_RETURN_GAP_WITHIN_LIMIT",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
            "MARGIN_BUFFER_WITHIN_LIMIT",
            *(
                (
                    "AUTHORITATIVE_ACCOUNT_BOUNDARIES_WITHIN_DELAY",
                    "OBSERVATION_START_EQUITY_FROM_ACCOUNT_LEDGER",
                )
                if bounded_boundaries
                else ()
            ),
        )
    )
    return EvaluationPlan(
        plan_id=spec.plan_id,
        registered_at=registered_at,
        base_manifest_id=spec.release_manifest_id,
        primary_metric="annualized_net_equity_return_lower_bound_vs_cash",
        minimum_sample_size=spec.thresholds.calendar_months,
        hard_guardrails=hard_guardrails,
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.SHADOW,
        ),
        fixed_regression_suite_version=(
            "investment-manager-capital-shadow-regression-v1"
            if legacy
            else (
                "investment-manager-capital-shadow-regression-v3"
                if bounded_boundaries
                else "investment-manager-capital-shadow-regression-v2"
            )
        ),
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=0,
    )


def evaluate_capital_shadow_plan(
    *,
    spec: CapitalShadowEvaluationSpec,
    plan: EvaluationPlan,
    projection: CapitalLedgerProjection | None,
    published_at: datetime,
) -> CapitalShadowEvaluationResult:
    """Apply only the preregistered gates to one immutable ledger projection."""

    published = require_utc(published_at)
    expected_plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=plan.registered_at,
    )
    if plan != expected_plan:
        raise ValueError("Capital evaluator 收到未预登记或已漂移的计划")
    mature_at = spec.observation_end + timedelta(days=spec.settlement_grace_days)
    if published < mature_at:
        return _capital_evaluation_result(
            spec=spec,
            published_at=published,
            source_hash=content_hash(
                {
                    "plan_id": spec.plan_id,
                    "published_at": published,
                    "mature_at": mature_at,
                }
            ),
            status=CapitalShadowEvaluationStatus.INCOMPLETE,
            metrics=None,
            reason_codes=("WINDOW_OR_SETTLEMENT_GRACE_NOT_MATURE",),
            source_ids=(),
        )
    if projection is None:
        return _capital_evaluation_result(
            spec=spec,
            published_at=published,
            source_hash=content_hash(
                {
                    "plan_id": spec.plan_id,
                    "published_at": published,
                    "missing": "CAPITAL_LEDGER_PROJECTION",
                }
            ),
            status=CapitalShadowEvaluationStatus.INCOMPLETE,
            metrics=None,
            reason_codes=("CAPITAL_LEDGER_PROJECTION_MISSING",),
            source_ids=(),
        )
    if projection.plan_id != spec.plan_id or projection.projected_at > published:
        raise ValueError("Capital ledger projection 不属于当前计划或含未来事实")
    months = spec.thresholds.calendar_months
    if (
        len(projection.monthly_net_return_fractions) != months
        or projection.source_counterfactual_annualized_return_fraction is None
    ):
        return _capital_evaluation_result(
            spec=spec,
            published_at=published,
            source_hash=projection.source_hash,
            status=CapitalShadowEvaluationStatus.INCOMPLETE,
            metrics=None,
            reason_codes=("MONTHLY_LEDGER_OR_COUNTERFACTUAL_INCOMPLETE",),
            source_ids=projection.source_ids,
        )
    if (
        spec.version != "capital-shadow-evaluation-spec-v4"
        and projection.starting_equity != spec.starting_equity
    ):
        raise ValueError("Capital ledger projection 起始权益与预登记合同不一致")
    monthly = projection.monthly_net_return_fractions
    monthly_log_returns = tuple((Decimal("1") + item).ln() for item in monthly)
    annualized = (
        sum(monthly_log_returns, Decimal("0")) / Decimal(months) * Decimal("12")
    ).exp() - Decimal("1")
    lower_bound = (
        conservative_newey_west_lower_bound(
            monthly_log_returns,
            z=spec.thresholds.lower_confidence_z,
            lag=spec.thresholds.newey_west_lag_months,
        )
        * Decimal("12")
    ).exp() - Decimal("1")
    counterfactual = projection.source_counterfactual_annualized_return_fraction
    underperformance = max(Decimal("0"), counterfactual - annualized)
    positive_months = sum(item > 0 for item in monthly)
    metrics = CapitalShadowEvaluationMetrics(
        calendar_months=months,
        forecast_available_months=projection.forecast_available_months,
        decision_complete_months=projection.decision_complete_months,
        positive_months=positive_months,
        annualized_net_return_fraction=annualized,
        annualized_net_return_lower_bound=lower_bound,
        source_counterfactual_annualized_return_fraction=counterfactual,
        source_policy_underperformance_fraction=underperformance,
        starting_equity=projection.starting_equity,
        ending_equity=projection.ending_equity,
        net_pnl=projection.net_pnl,
        price_pnl=projection.price_pnl,
        funding_pnl=projection.funding_pnl,
        fee_cost=projection.fee_cost,
        execution_slippage_cost=projection.execution_slippage_cost,
        compensation_loss=projection.compensation_loss,
        maximum_drawdown_fraction=projection.maximum_drawdown_fraction,
        minimum_margin_buffer_fraction=projection.minimum_margin_buffer_fraction,
        late_entry_count=projection.late_entry_count,
        duplicate_execution_group_count=(projection.duplicate_execution_group_count),
        unresolved_execution_group_count=(projection.unresolved_execution_group_count),
        maximum_unhedged_seconds=projection.maximum_unhedged_seconds,
        maximum_group_recovery_seconds=projection.maximum_group_recovery_seconds,
    )
    thresholds = spec.thresholds
    reasons = []
    if projection.forecast_available_months < thresholds.minimum_forecast_available_months:
        reasons.append("FORECAST_AVAILABLE_MONTHS_BELOW_GATE")
    if projection.decision_complete_months < thresholds.minimum_decision_complete_months:
        reasons.append("CAPITAL_DECISION_MONTHS_INCOMPLETE")
    if positive_months < thresholds.minimum_positive_months:
        reasons.append("POSITIVE_MONTHS_BELOW_GATE")
    if projection.late_entry_count > thresholds.maximum_late_entries:
        reasons.append("LATE_ENTRY_DETECTED")
    if projection.duplicate_execution_group_count > thresholds.maximum_duplicate_execution_groups:
        reasons.append("DUPLICATE_EXECUTION_GROUP_DETECTED")
    if projection.unresolved_execution_group_count:
        reasons.append("UNRESOLVED_EXECUTION_GROUP")
    if projection.maximum_unhedged_seconds > thresholds.maximum_unhedged_seconds:
        reasons.append("UNHEDGED_DURATION_EXCEEDED")
    if projection.maximum_group_recovery_seconds > thresholds.maximum_group_recovery_seconds:
        reasons.append("GROUP_RECOVERY_DURATION_EXCEEDED")
    if lower_bound <= thresholds.minimum_annualized_return_lower_bound:
        reasons.append("ANNUALIZED_RETURN_LOWER_BOUND_NOT_POSITIVE")
    if underperformance > thresholds.maximum_source_policy_underperformance_fraction:
        reasons.append("SOURCE_COUNTERFACTUAL_UNDERPERFORMANCE_EXCEEDED")
    if projection.maximum_drawdown_fraction > thresholds.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if projection.minimum_margin_buffer_fraction < thresholds.minimum_margin_buffer_fraction:
        reasons.append("MARGIN_BUFFER_BELOW_GATE")
    reason_codes = tuple(sorted(reasons))
    return _capital_evaluation_result(
        spec=spec,
        published_at=published,
        source_hash=projection.source_hash,
        status=(
            CapitalShadowEvaluationStatus.FAILED
            if reason_codes
            else CapitalShadowEvaluationStatus.PASSED
        ),
        metrics=metrics,
        reason_codes=reason_codes,
        source_ids=projection.source_ids,
    )


def _capital_evaluation_result(
    *,
    spec: CapitalShadowEvaluationSpec,
    published_at: datetime,
    source_hash: str,
    status: CapitalShadowEvaluationStatus,
    metrics: CapitalShadowEvaluationMetrics | None,
    reason_codes: tuple[str, ...],
    source_ids: tuple[str, ...],
) -> CapitalShadowEvaluationResult:
    spec_hash = content_hash(spec)
    normalized_reasons = tuple(sorted(set(reason_codes)))
    normalized_sources = tuple(sorted(set(source_ids)))
    result_id = stable_id(
        "capital_shadow_evaluation",
        spec.plan_id,
        spec_hash,
        published_at,
        source_hash,
        status,
        metrics,
        normalized_reasons,
        normalized_sources,
    )
    return CapitalShadowEvaluationResult(
        result_id=result_id,
        plan_id=spec.plan_id,
        evaluation_spec_hash=spec_hash,
        published_at=published_at,
        source_hash=source_hash,
        status=status,
        metrics=metrics,
        reason_codes=normalized_reasons,
        source_ids=normalized_sources,
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
        if not isinstance(snapshot, dict) or snapshot.get("version") not in {
            "capital-shadow-evaluation-spec-v1",
            "capital-shadow-evaluation-spec-v2",
            "capital-shadow-evaluation-spec-v3",
            "capital-shadow-evaluation-spec-v4",
        }:
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
