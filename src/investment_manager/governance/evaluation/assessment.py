from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.context.settlement import AssessmentViewOutcome
from investment_manager.forecast.models import ForecastOutcomeStatus
from investment_manager.governance.models import (
    EvaluationPlan,
    EvaluationStage,
    FailedExperiment,
    ReleaseManifest,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.settings import AppConfig


class AssessmentEvaluationScope(FrozenModel):
    asset: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,32}$")
    horizon_minutes: int = Field(gt=0)


class AssessmentForwardEvaluationSpec(FrozenModel):
    """Immutable, result-before-known contract for ContextAssessment outcomes."""

    version: Literal["context-assessment-forward-spec-v1"] = (
        "context-assessment-forward-spec-v1"
    )
    plan_id: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_evaluation_version: str = Field(min_length=1)
    signal_window_start: datetime
    signal_window_end: datetime
    scopes: tuple[AssessmentEvaluationScope, ...] = Field(min_length=1)
    minimum_non_overlapping_samples: int = Field(default=30, ge=2)
    settlement_grace_minutes: int = Field(default=120, ge=0, le=1440)
    lower_confidence_z: Decimal = Field(default=Decimal("1.96"), gt=0)
    baseline_id: Literal["always-up-on-all-scoreable-timestamps-v1"] = (
        "always-up-on-all-scoreable-timestamps-v1"
    )

    _utc_signal_start = field_validator("signal_window_start")(require_utc)
    _utc_signal_end = field_validator("signal_window_end")(require_utc)

    @model_validator(mode="after")
    def scope_and_window_are_canonical_and_feasible(self):
        if not self.signal_window_start < self.signal_window_end:
            raise ValueError("ContextAssessment 前向窗口起点必须早于终点")
        keys = tuple(
            (item.asset, item.symbol, item.horizon_minutes) for item in self.scopes
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("ContextAssessment 前向作用域必须唯一且排序")
        required_span = timedelta(
            minutes=max(item.horizon_minutes for item in self.scopes)
            * self.minimum_non_overlapping_samples
        )
        if self.signal_window_end - self.signal_window_start < required_span:
            raise ValueError("ContextAssessment 前向窗口不足以形成预登记非重叠样本")
        return self


class AssessmentScopeMetrics(FrozenModel):
    asset: str
    symbol: str
    horizon_minutes: int = Field(gt=0)
    outcome_count: int = Field(ge=0)
    scoreable_count: int = Field(ge=0)
    settled_direction_count: int = Field(ge=0)
    abstained_count: int = Field(ge=0)
    unscorable_count: int = Field(ge=0)
    non_overlapping_scoreable_count: int = Field(ge=0)
    correct_direction_count: int = Field(ge=0)
    directional_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    abstention_fraction: Decimal | None = Field(default=None, ge=0, le=1)
    average_strategy_return_bps: Decimal | None = None
    strategy_return_bps_lower_bound: Decimal | None = None
    baseline_id: str
    always_up_average_return_bps: Decimal | None = None
    average_return_delta_bps_vs_always_up: Decimal | None = None
    return_delta_bps_lower_bound_vs_always_up: Decimal | None = None
    sample_sufficient: bool


class AssessmentForwardOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class AssessmentForwardEvaluationResult(FrozenModel):
    version: Literal["context-assessment-forward-result-v2"] = (
        "context-assessment-forward-result-v2"
    )
    result_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str
    published_at: datetime
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: tuple[AssessmentScopeMetrics, ...] = Field(min_length=1)
    outcome_ids: tuple[str, ...]
    outcome: AssessmentForwardOutcome
    reason_codes: tuple[str, ...]
    limitations: tuple[str, ...] = (
        "DIRECTIONAL_ASSESSMENT_ONLY_NOT_TRADABLE_PNL",
        "NO_FEES_OR_POSITION_SIZING_IN_THIS_GATE",
        "CODEX_COMPLETION_RELIABILITY_EVALUATED_SEPARATELY",
    )

    _utc_published_at = field_validator("published_at")(require_utc)

    @model_validator(mode="after")
    def identity_matches_payload(self):
        expected = stable_id(
            "context_assessment_forward_evaluation",
            self.evaluation_spec_hash,
            self.published_at,
            self.source_hash,
            self.scopes,
            self.outcome_ids,
            self.outcome,
            self.reason_codes,
            self.limitations,
        )
        if self.result_id != expected:
            raise ValueError("ContextAssessment 前向评价结果身份不一致")
        return self


class _AssessmentForwardEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: AssessmentForwardEvaluationResult


class AssessmentForwardEvaluationCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: AssessmentForwardEvaluationResult) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一 ContextAssessment 前向结果 ID 的内容不一致")
            return target
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".context-assessment-forward-",
            payload=_AssessmentForwardEnvelope(
                result_hash=content_hash(result),
                result=result,
            ),
        )

    def load(self, result_id: str) -> AssessmentForwardEvaluationResult:
        raw = json.loads(
            (self._root / f"{result_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(
            raw.get("result")
        ):
            raise ValueError("ContextAssessment 前向评价制品内容哈希不匹配")
        envelope = _AssessmentForwardEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("ContextAssessment 前向评价文件名与结果 ID 不一致")
        return envelope.result


def build_assessment_forward_plan(
    *,
    spec: AssessmentForwardEvaluationSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    registered = require_utc(registered_at)
    if registered >= spec.signal_window_start:
        raise ValueError("ContextAssessment 前向计划必须在首个信号生成前登记")
    return EvaluationPlan(
        plan_id=spec.plan_id,
        registered_at=registered,
        base_manifest_id=base_manifest_id,
        primary_metric="return_delta_bps_lower_bound_vs_always_up_each_scope",
        minimum_sample_size=(
            spec.minimum_non_overlapping_samples * len(spec.scopes)
        ),
        hard_guardrails=(
            "NON_OVERLAPPING_SCOREABLE_SAMPLE_SUFFICIENT_EACH_SCOPE",
            "PAIRED_RETURN_DELTA_LOWER_BOUND_POSITIVE_EACH_SCOPE",
            "NO_PENDING_ASSESSMENT_IN_SIGNAL_WINDOW",
        ),
        required_stages=(EvaluationStage.SHADOW,),
        fixed_regression_suite_version=(
            "context-assessment-forward-regression-v1"
        ),
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
    )


def validate_assessment_forward_plan(
    *,
    spec: AssessmentForwardEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
    published_at: datetime,
) -> None:
    published = require_utc(published_at)
    expected = build_assessment_forward_plan(
        spec=spec,
        base_manifest_id=champion_manifest_id,
        registered_at=plan.registered_at,
    )
    if plan != expected:
        raise ValueError("ContextAssessment EvaluationPlan 与预登记合同不一致")
    complete_after = spec.signal_window_end + timedelta(
        minutes=max(item.horizon_minutes for item in spec.scopes)
        + spec.settlement_grace_minutes
    )
    if published < complete_after:
        raise ValueError("ContextAssessment 前向窗口尚未完整到期并经过结算宽限")


def validate_assessment_runtime_plan(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    plans: tuple[EvaluationPlan, ...],
    started_at: datetime,
) -> tuple[AssessmentForwardEvaluationSpec, EvaluationPlan]:
    """Require one exact preregistered Context behavior before worker startup."""

    from investment_manager.forecast.context.analyst import (
        configured_assess_behavior_hash,
    )

    started = require_utc(started_at)
    mandate = config.assessment.mandate
    expected_scopes = tuple(
        sorted(
            (
                AssessmentEvaluationScope(
                    asset=asset.asset,
                    symbol=asset.market_symbol,
                    horizon_minutes=horizon,
                )
                for asset in mandate.assets
                for horizon in asset.horizons_minutes
            ),
            key=lambda item: (item.asset, item.symbol, item.horizon_minutes),
        )
    )
    candidates: list[tuple[AssessmentForwardEvaluationSpec, EvaluationPlan]] = []
    for plan in plans:
        snapshot = plan.candidate_spec_snapshot
        if not isinstance(snapshot, dict) or snapshot.get("version") != (
            "context-assessment-forward-spec-v1"
        ):
            continue
        try:
            spec = AssessmentForwardEvaluationSpec.model_validate(snapshot)
            expected_plan = build_assessment_forward_plan(
                spec=spec,
                base_manifest_id=manifest.manifest_id,
                registered_at=plan.registered_at,
            )
        except ValueError as exc:
            raise ValueError("ContextAssessment EvaluationPlan 不是完整预登记合同") from exc
        if plan != expected_plan:
            raise ValueError("ContextAssessment EvaluationPlan 与运行 Release 不一致")
        if (
            spec.analysis_scope != mandate.analysis_scope
            or spec.analysis_behavior_hash != configured_assess_behavior_hash(config)
            or spec.outcome_evaluation_version
            != config.outcome_evaluation.assessment_version
            or spec.settlement_grace_minutes
            != config.outcome_evaluation.settlement_grace_minutes
            or spec.scopes != expected_scopes
        ):
            raise ValueError("ContextAssessment EvaluationPlan 与当前行为不一致")
        if plan.registered_at > started:
            raise ValueError("ContextAssessment EvaluationPlan 晚于本次服务启动")
        if started >= spec.signal_window_end:
            raise ValueError("ContextAssessment Worker 启动时预登记信号窗口已结束")
        candidates.append((spec, plan))
    if len(candidates) != 1:
        raise ValueError("ContextAssessment Release 必须恰好绑定一个当前行为计划")
    return candidates[0]


def evaluate_assessment_forward_plan(
    *,
    spec: AssessmentForwardEvaluationSpec,
    outcomes: tuple[AssessmentViewOutcome, ...],
    published_at: datetime,
) -> AssessmentForwardEvaluationResult:
    published = require_utc(published_at)
    scope_keys = {
        (item.asset, item.symbol, item.horizon_minutes) for item in spec.scopes
    }
    if any(
        item.analysis_scope != spec.analysis_scope
        or item.analysis_behavior_hash != spec.analysis_behavior_hash
        or item.evaluation_version != spec.outcome_evaluation_version
        or (item.asset, item.symbol, item.horizon_minutes) not in scope_keys
        or not spec.signal_window_start
        <= item.signal_observed_at
        < spec.signal_window_end
        or item.settled_at > published
        for item in outcomes
    ):
        raise ValueError("ContextAssessment 前向评价包含预登记作用域外结果")
    outcome_ids = tuple(item.outcome_id for item in outcomes)
    view_keys = tuple(
        (item.assessment_id, item.asset, item.horizon_minutes) for item in outcomes
    )
    if len(set(outcome_ids)) != len(outcome_ids) or len(set(view_keys)) != len(
        view_keys
    ):
        raise ValueError("ContextAssessment 前向评价结果不得重复")

    ordered = tuple(
        sorted(outcomes, key=lambda item: (item.signal_observed_at, item.outcome_id))
    )
    metrics = tuple(
        _scope_metrics(
            scope,
            tuple(
                item
                for item in ordered
                if (item.asset, item.symbol, item.horizon_minutes)
                == (scope.asset, scope.symbol, scope.horizon_minutes)
            ),
            minimum_samples=spec.minimum_non_overlapping_samples,
            lower_confidence_z=spec.lower_confidence_z,
            baseline_id=spec.baseline_id,
        )
        for scope in spec.scopes
    )
    evidence_incomplete_reasons: list[str] = []
    if any(item.outcome_count == 0 for item in metrics):
        evidence_incomplete_reasons.append("EXPECTED_SCOPE_MISSING")
    if any(not item.sample_sufficient for item in metrics):
        evidence_incomplete_reasons.append("NON_OVERLAPPING_SAMPLE_TOO_SMALL")
    if evidence_incomplete_reasons:
        outcome = AssessmentForwardOutcome.INCONCLUSIVE
        reason_codes = tuple(evidence_incomplete_reasons)
    elif any(
        item.return_delta_bps_lower_bound_vs_always_up is None
        or item.return_delta_bps_lower_bound_vs_always_up <= 0
        for item in metrics
    ):
        outcome = AssessmentForwardOutcome.FAILED
        reason_codes = ("PAIRED_RETURN_DELTA_LOWER_BOUND_NOT_POSITIVE",)
    else:
        outcome = AssessmentForwardOutcome.PASSED
        reason_codes = ()
    source_hash = content_hash(
        [item.model_dump(mode="json") for item in ordered]
    )
    spec_hash = content_hash(spec)
    limitations = (
        "DIRECTIONAL_ASSESSMENT_ONLY_NOT_TRADABLE_PNL",
        "NO_FEES_OR_POSITION_SIZING_IN_THIS_GATE",
        "CODEX_COMPLETION_RELIABILITY_EVALUATED_SEPARATELY",
    )
    result_id = stable_id(
        "context_assessment_forward_evaluation",
        spec_hash,
        published,
        source_hash,
        metrics,
        tuple(item.outcome_id for item in ordered),
        outcome,
        reason_codes,
        limitations,
    )
    return AssessmentForwardEvaluationResult(
        result_id=result_id,
        evaluation_spec_hash=spec_hash,
        plan_id=spec.plan_id,
        published_at=published,
        source_hash=source_hash,
        scopes=metrics,
        outcome_ids=tuple(item.outcome_id for item in ordered),
        outcome=outcome,
        reason_codes=reason_codes,
        limitations=limitations,
    )


def failed_assessment_forward_experiment(
    result: AssessmentForwardEvaluationResult,
    *,
    rejected_at: datetime,
) -> FailedExperiment:
    if result.outcome != AssessmentForwardOutcome.FAILED:
        raise ValueError("只有证据充分且未通过的前向评价才能登记为失败实验")
    hypothesis = (
        f"ContextAssessment 计划 {result.plan_id} 在全部资产和周期上相对 "
        "always-UP 的配对收益增量保守下界为正且样本充分"
    )
    return FailedExperiment(
        experiment_id=stable_id(
            "failed_context_assessment_forward",
            result.result_id,
        ),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(f"hypothesis:{hypothesis}", result.result_id),
        rejected_at=require_utc(rejected_at),
        reason_codes=("CONTEXT_ASSESSMENT_FORWARD_FAILED", *result.reason_codes),
    )


def _scope_metrics(
    scope: AssessmentEvaluationScope,
    outcomes: tuple[AssessmentViewOutcome, ...],
    *,
    minimum_samples: int,
    lower_confidence_z: Decimal,
    baseline_id: str,
) -> AssessmentScopeMetrics:
    scoreable = tuple(
        item
        for item in outcomes
        if item.status
        in (ForecastOutcomeStatus.SETTLED, ForecastOutcomeStatus.ABSTAINED)
    )
    independent = _non_overlapping(scoreable)
    strategy_returns = tuple(_strategy_return(item) for item in independent)
    market_returns = tuple(_market_return(item) for item in independent)
    deltas = tuple(
        strategy - baseline
        for strategy, baseline in zip(
            strategy_returns,
            market_returns,
            strict=True,
        )
    )
    settled = tuple(
        item for item in independent if item.status == ForecastOutcomeStatus.SETTLED
    )
    correct = sum(item.direction_correct is True for item in settled)
    return AssessmentScopeMetrics(
        asset=scope.asset,
        symbol=scope.symbol,
        horizon_minutes=scope.horizon_minutes,
        outcome_count=len(outcomes),
        scoreable_count=len(scoreable),
        settled_direction_count=sum(
            item.status == ForecastOutcomeStatus.SETTLED for item in outcomes
        ),
        abstained_count=sum(
            item.status == ForecastOutcomeStatus.ABSTAINED for item in outcomes
        ),
        unscorable_count=sum(
            item.status == ForecastOutcomeStatus.UNSCORABLE for item in outcomes
        ),
        non_overlapping_scoreable_count=len(independent),
        correct_direction_count=correct,
        directional_accuracy=(
            Decimal(correct) / Decimal(len(settled)) if settled else None
        ),
        abstention_fraction=(
            Decimal(sum(item.status == ForecastOutcomeStatus.ABSTAINED for item in scoreable))
            / Decimal(len(scoreable))
            if scoreable
            else None
        ),
        average_strategy_return_bps=_mean(strategy_returns),
        strategy_return_bps_lower_bound=_mean_lower_bound(
            strategy_returns,
            z=lower_confidence_z,
        ),
        baseline_id=baseline_id,
        always_up_average_return_bps=_mean(market_returns),
        average_return_delta_bps_vs_always_up=_mean(deltas),
        return_delta_bps_lower_bound_vs_always_up=_mean_lower_bound(
            deltas,
            z=lower_confidence_z,
        ),
        sample_sufficient=len(independent) >= minimum_samples,
    )


def _non_overlapping(
    outcomes: tuple[AssessmentViewOutcome, ...],
) -> tuple[AssessmentViewOutcome, ...]:
    selected: list[AssessmentViewOutcome] = []
    last_evaluation_at: datetime | None = None
    for outcome in sorted(
        outcomes,
        key=lambda item: (
            item.signal_observed_at,
            item.evaluation_at,
            item.outcome_id,
        ),
    ):
        if (
            last_evaluation_at is not None
            and outcome.signal_observed_at < last_evaluation_at
        ):
            continue
        selected.append(outcome)
        last_evaluation_at = outcome.evaluation_at
    return tuple(selected)


def _strategy_return(outcome: AssessmentViewOutcome) -> Decimal:
    if outcome.status == ForecastOutcomeStatus.ABSTAINED:
        return Decimal("0")
    if outcome.directional_return_bps is None:
        raise ValueError("SETTLED ContextAssessment 缺少方向收益")
    return outcome.directional_return_bps


def _market_return(outcome: AssessmentViewOutcome) -> Decimal:
    if outcome.market_return_bps is None:
        raise ValueError("可评分 ContextAssessment 缺少市场收益")
    return outcome.market_return_bps


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _mean_lower_bound(
    values: tuple[Decimal, ...],
    *,
    z: Decimal,
) -> Decimal | None:
    if len(values) < 2:
        return None
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    variance = sum(((item - mean) ** 2 for item in values), Decimal("0")) / (
        count - 1
    )
    return mean - z * (variance / count).sqrt()
