from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from investment_manager.forecast.models import (
    BaseForecast,
    ContextAssessment,
    ContextCapitalEffect,
    ContextCapitalRelevanceStatus,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.forecast.tables import (
    context_assessments,
    forecast_outcomes,
    forecasts,
)
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
from investment_manager.platform.fact_store import analysis_behavior_not_quarantined
from investment_manager.settings import AppConfig


class ContextCapitalForwardSpec(FrozenModel):
    """Frozen pairing of one Context behavior with one program capital task."""

    version: Literal["context-capital-forward-spec-v1"] = "context-capital-forward-spec-v1"
    plan_id: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    objective_id: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    forecast_evaluation_version: str = Field(min_length=1)
    signal_window_start: datetime
    signal_window_end: datetime
    maximum_context_age_hours: int = Field(default=24, ge=1, le=168)
    minimum_opportunity_count: int = Field(default=12, ge=3)
    settlement_grace_days: int = Field(default=7, ge=0, le=31)
    round_trip_cost_bps: Decimal = Field(ge=0)
    lower_confidence_z: Decimal = Field(default=Decimal("1.96"), gt=0)
    pairing_rule: Literal["latest-prior-context-or-program-fallback-v1"] = (
        "latest-prior-context-or-program-fallback-v1"
    )

    _utc_signal_start = field_validator("signal_window_start")(require_utc)
    _utc_signal_end = field_validator("signal_window_end")(require_utc)

    @model_validator(mode="after")
    def window_is_ordered(self):
        if not self.signal_window_start < self.signal_window_end:
            raise ValueError("Context Capital 前向窗口起点必须早于终点")
        return self


class ContextCapitalOpportunity(FrozenModel):
    forecast_id: str = Field(min_length=1)
    assessment_id: str | None = None
    context_status: ContextCapitalRelevanceStatus | ContextCapitalEffect | None = None
    available_at: datetime
    base_net_return_bps: Decimal
    context_net_return_bps: Decimal
    return_delta_bps: Decimal
    used_program_fallback: bool

    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def paired_returns_are_consistent(self):
        if self.return_delta_bps != (self.context_net_return_bps - self.base_net_return_bps):
            raise ValueError("Context Capital 配对收益差无法核对")
        expected_fallback = self.assessment_id is None
        if self.used_program_fallback != expected_fallback:
            raise ValueError("Context Capital fallback 身份不一致")
        if expected_fallback != (self.context_status is None):
            raise ValueError("Context Capital Assessment 引用不完整")
        return self


class ContextCapitalForwardOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ContextCapitalForwardResult(FrozenModel):
    version: Literal["context-capital-forward-result-v1"] = "context-capital-forward-result-v1"
    result_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    published_at: datetime
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opportunities: tuple[ContextCapitalOpportunity, ...]
    incomplete_forecast_ids: tuple[str, ...]
    natural_opportunity_count: int = Field(ge=0)
    paired_opportunity_count: int = Field(ge=0)
    veto_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    base_average_net_return_bps: Decimal | None = None
    context_average_net_return_bps: Decimal | None = None
    average_return_delta_bps: Decimal | None = None
    return_delta_lower_bound_bps: Decimal | None = None
    outcome: ContextCapitalForwardOutcome
    reason_codes: tuple[str, ...]
    limitations: tuple[str, ...] = (
        "SHADOW_COUNTERFACTUAL_NOT_LIVE_AUTHORITY",
        "CONTEXT_CAN_ONLY_VETO_PROGRAM_ENTRY_NOT_SIZE_OR_CREATE_TRADES",
        "PROGRAM_FALLBACK_ON_MISSING_OR_STALE_CONTEXT",
    )

    _utc_published_at = field_validator("published_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_counts_are_consistent(self):
        if self.paired_opportunity_count != len(self.opportunities):
            raise ValueError("Context Capital paired_opportunity_count 不一致")
        if self.natural_opportunity_count != (
            self.paired_opportunity_count + len(self.incomplete_forecast_ids)
        ):
            raise ValueError("Context Capital natural_opportunity_count 不一致")
        if self.veto_count != sum(
            _is_entry_veto(item.context_status) for item in self.opportunities
        ):
            raise ValueError("Context Capital veto_count 不一致")
        if self.fallback_count != sum(item.used_program_fallback for item in self.opportunities):
            raise ValueError("Context Capital fallback_count 不一致")
        expected = stable_id(
            "context_capital_forward_evaluation",
            self.evaluation_spec_hash,
            self.published_at,
            self.source_hash,
            self.opportunities,
            self.incomplete_forecast_ids,
            self.outcome,
            self.reason_codes,
            self.limitations,
        )
        if self.result_id != expected:
            raise ValueError("Context Capital 前向结果身份不一致")
        return self


class _ContextCapitalEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: ContextCapitalForwardResult


class ContextCapitalForwardCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: ContextCapitalForwardResult) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一 Context Capital 结果 ID 的内容不一致")
            return target
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".context-capital-forward-",
            payload=_ContextCapitalEnvelope(
                result_hash=content_hash(result),
                result=result,
            ),
        )

    def load(self, result_id: str) -> ContextCapitalForwardResult:
        raw = json.loads((self._root / f"{result_id}.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(raw.get("result")):
            raise ValueError("Context Capital 前向制品内容哈希不匹配")
        envelope = _ContextCapitalEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("Context Capital 前向文件名与结果 ID 不一致")
        return envelope.result


def build_context_capital_forward_plan(
    *,
    spec: ContextCapitalForwardSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    registered = require_utc(registered_at)
    if registered >= spec.signal_window_start:
        raise ValueError("Context Capital 前向计划必须在首个自然机会前登记")
    return EvaluationPlan(
        plan_id=spec.plan_id,
        registered_at=registered,
        base_manifest_id=base_manifest_id,
        primary_metric="paired_net_return_delta_lower_bound_bps",
        minimum_sample_size=spec.minimum_opportunity_count,
        hard_guardrails=(
            "NATURAL_OPPORTUNITY_COUNT_SUFFICIENT",
            "NO_INCOMPLETE_PROGRAM_FORECAST_OUTCOMES",
            "PAIRED_NET_RETURN_DELTA_LOWER_BOUND_POSITIVE",
        ),
        required_stages=(EvaluationStage.SHADOW,),
        fixed_regression_suite_version="context-capital-forward-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
    )


def validate_context_capital_forward_plan(
    *,
    spec: ContextCapitalForwardSpec,
    plan: EvaluationPlan,
    base_manifest_id: str,
    published_at: datetime,
) -> None:
    publication = require_utc(published_at)
    expected = build_context_capital_forward_plan(
        spec=spec,
        base_manifest_id=base_manifest_id,
        registered_at=plan.registered_at,
    )
    if plan != expected:
        raise ValueError("Context Capital EvaluationPlan 与预登记合同不一致")
    complete_after = spec.signal_window_end + timedelta(days=31 + spec.settlement_grace_days)
    if publication < complete_after:
        raise ValueError("Context Capital 前向窗口尚未完整结算")


def validate_context_capital_runtime_plan(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    plans: tuple[EvaluationPlan, ...],
    started_at: datetime,
) -> tuple[ContextCapitalForwardSpec, EvaluationPlan]:
    from investment_manager.forecast.context.analyst import (
        configured_assess_behavior_hash,
    )

    started = require_utc(started_at)
    objective = config.assessment.mandate.capital_objective
    if objective is None:
        raise ValueError("当前 Context 行为没有资本目标")
    candidates: list[tuple[ContextCapitalForwardSpec, EvaluationPlan]] = []
    for plan in plans:
        snapshot = plan.candidate_spec_snapshot
        if not isinstance(snapshot, dict) or snapshot.get("version") != (
            "context-capital-forward-spec-v1"
        ):
            continue
        spec = ContextCapitalForwardSpec.model_validate(snapshot)
        expected = build_context_capital_forward_plan(
            spec=spec,
            base_manifest_id=manifest.manifest_id,
            registered_at=plan.registered_at,
        )
        if plan != expected:
            raise ValueError("Context Capital EvaluationPlan 与运行 Release 不一致")
        if (
            spec.analysis_scope != config.assessment.mandate.analysis_scope
            or spec.analysis_behavior_hash != configured_assess_behavior_hash(config)
            or spec.objective_id != objective.objective_id
            or spec.producer_id != objective.producer_id
            or spec.producer_version != objective.producer_version
            or spec.forecast_family != objective.forecast_family
            or spec.forecast_evaluation_version != config.outcome_evaluation.forecast_version
        ):
            raise ValueError("Context Capital EvaluationPlan 与当前行为不一致")
        if plan.registered_at > started:
            raise ValueError("Context Capital EvaluationPlan 晚于本次服务启动")
        if started >= spec.signal_window_end:
            raise ValueError("Context Capital Worker 启动时前向信号窗口已结束")
        candidates.append((spec, plan))
    if len(candidates) != 1:
        raise ValueError("Context Capital Release 必须恰好绑定一个当前行为计划")
    return candidates[0]


def load_context_capital_inputs(
    *,
    context_engine: Engine,
    capital_engine: Engine,
    spec: ContextCapitalForwardSpec,
    published_at: datetime,
) -> tuple[
    tuple[tuple[BaseForecast, ForecastOutcome], ...],
    tuple[ContextAssessment, ...],
    tuple[str, ...],
]:
    published = require_utc(published_at)
    joined = forecasts.outerjoin(
        forecast_outcomes,
        and_(
            forecasts.c.forecast_id == forecast_outcomes.c.forecast_id,
            forecast_outcomes.c.evaluation_version == spec.forecast_evaluation_version,
        ),
    )
    with capital_engine.connect() as connection:
        rows = connection.execute(
            select(forecasts.c.payload, forecast_outcomes.c.payload)
            .select_from(joined)
            .where(
                forecasts.c.kind == "BASE",
                forecasts.c.producer_id == spec.producer_id,
                forecasts.c.producer_version == spec.producer_version,
                forecasts.c.forecast_family == spec.forecast_family,
                forecasts.c.available_at >= spec.signal_window_start,
                forecasts.c.available_at < spec.signal_window_end,
            )
            .order_by(forecasts.c.available_at, forecasts.c.forecast_id)
        ).all()
    settled: list[tuple[BaseForecast, ForecastOutcome]] = []
    incomplete: list[str] = []
    for forecast_payload, outcome_payload in rows:
        forecast = BaseForecast.model_validate(forecast_payload)
        if outcome_payload is None:
            incomplete.append(forecast.forecast_id)
            continue
        outcome = ForecastOutcome.model_validate(outcome_payload)
        if (
            outcome.status != ForecastOutcomeStatus.SETTLED
            or outcome.settled_at is None
            or outcome.settled_at > published
            or outcome.gross_target_return_bps is None
        ):
            incomplete.append(forecast.forecast_id)
            continue
        settled.append((forecast, outcome))
    context_start = spec.signal_window_start - timedelta(hours=spec.maximum_context_age_hours)
    with context_engine.connect() as connection:
        payloads = connection.execute(
            select(context_assessments.c.payload)
            .where(
                context_assessments.c.analysis_behavior_hash == spec.analysis_behavior_hash,
                analysis_behavior_not_quarantined(context_assessments.c.analysis_behavior_hash),
                context_assessments.c.available_at >= context_start,
                context_assessments.c.available_at < spec.signal_window_end,
                context_assessments.c.available_at <= published,
            )
            .order_by(
                context_assessments.c.available_at,
                context_assessments.c.assessment_id,
            )
        ).scalars()
        assessments = tuple(ContextAssessment.model_validate(item) for item in payloads)
    return tuple(settled), assessments, tuple(sorted(incomplete))


def evaluate_context_capital_forward_plan(
    *,
    spec: ContextCapitalForwardSpec,
    forecasts_and_outcomes: tuple[tuple[BaseForecast, ForecastOutcome], ...],
    assessments: tuple[ContextAssessment, ...],
    incomplete_forecast_ids: tuple[str, ...],
    published_at: datetime,
) -> ContextCapitalForwardResult:
    published = require_utc(published_at)
    forecast_ids = tuple(item.forecast_id for item, _ in forecasts_and_outcomes)
    if len(set(forecast_ids)) != len(forecast_ids):
        raise ValueError("Context Capital 不能重复评价 Program Forecast")
    if tuple(sorted(set(incomplete_forecast_ids))) != incomplete_forecast_ids:
        raise ValueError("Context Capital incomplete Forecast 必须唯一且排序")
    if set(forecast_ids).intersection(incomplete_forecast_ids):
        raise ValueError("Context Capital 完整与不完整 Forecast cohort 重叠")
    for forecast, outcome in forecasts_and_outcomes:
        if (
            forecast.producer_id != spec.producer_id
            or forecast.producer_version != spec.producer_version
            or forecast.forecast_family != spec.forecast_family
            or not spec.signal_window_start <= forecast.available_at < spec.signal_window_end
            or outcome.forecast_id != forecast.forecast_id
            or outcome.evaluation_version != spec.forecast_evaluation_version
        ):
            raise ValueError("Context Capital 输入不属于预登记 Program cohort")
    assessment_ids = tuple(item.assessment_id for item in assessments)
    if len(set(assessment_ids)) != len(assessment_ids):
        raise ValueError("Context Capital 不能重复读取 Assessment")
    if any(
        item.analysis_scope != spec.analysis_scope
        or item.analysis_behavior_hash != spec.analysis_behavior_hash
        for item in assessments
    ):
        raise ValueError("Context Capital Assessment 不属于预登记行为 cohort")
    ordered_assessments = tuple(
        sorted(assessments, key=lambda item: (item.available_at, item.assessment_id))
    )
    opportunities: list[ContextCapitalOpportunity] = []
    for forecast, outcome in forecasts_and_outcomes:
        if (
            outcome.status != ForecastOutcomeStatus.SETTLED
            or outcome.settled_at is None
            or outcome.settled_at > published
            or outcome.gross_target_return_bps is None
        ):
            raise ValueError("Context Capital 完整 cohort 含不可结算 Outcome")
        eligible = tuple(
            item
            for item in ordered_assessments
            if item.available_at <= forecast.available_at
            and forecast.available_at - item.available_at
            <= timedelta(hours=spec.maximum_context_age_hours)
            and _capital_objective_id(item) == spec.objective_id
        )
        assessment = eligible[-1] if eligible else None
        base_net = outcome.gross_target_return_bps - spec.round_trip_cost_bps
        context_status = _capital_status(assessment)
        veto = _is_entry_veto(context_status)
        context_net = Decimal("0") if veto else base_net
        opportunities.append(
            ContextCapitalOpportunity(
                forecast_id=forecast.forecast_id,
                assessment_id=(assessment.assessment_id if assessment is not None else None),
                context_status=context_status,
                available_at=forecast.available_at,
                base_net_return_bps=base_net,
                context_net_return_bps=context_net,
                return_delta_bps=context_net - base_net,
                used_program_fallback=assessment is None,
            )
        )
    paired = tuple(opportunities)
    deltas = tuple(item.return_delta_bps for item in paired)
    base_mean = _mean(tuple(item.base_net_return_bps for item in paired))
    context_mean = _mean(tuple(item.context_net_return_bps for item in paired))
    delta_mean = _mean(deltas)
    delta_lower = _lower_bound(deltas, spec.lower_confidence_z)
    reasons: set[str] = set()
    if incomplete_forecast_ids:
        reasons.add("PROGRAM_FORECAST_OUTCOMES_INCOMPLETE")
    if len(paired) < spec.minimum_opportunity_count:
        reasons.add("NATURAL_OPPORTUNITY_COUNT_INSUFFICIENT")
    if delta_lower is None or delta_lower <= 0:
        reasons.add("PAIRED_NET_RETURN_DELTA_LOWER_BOUND_NOT_POSITIVE")
    if incomplete_forecast_ids or len(paired) < spec.minimum_opportunity_count:
        outcome = ContextCapitalForwardOutcome.INCONCLUSIVE
    elif delta_lower is None or delta_lower <= 0:
        outcome = ContextCapitalForwardOutcome.FAILED
    else:
        outcome = ContextCapitalForwardOutcome.PASSED
    source_hash = content_hash(
        {
            "forecasts": tuple(item.forecast_id for item, _ in forecasts_and_outcomes),
            "outcomes": tuple(item.outcome_id for _, item in forecasts_and_outcomes),
            "assessments": tuple(item.assessment_id for item in ordered_assessments),
            "incomplete": incomplete_forecast_ids,
        }
    )
    reason_codes = tuple(sorted(reasons))
    result_id = stable_id(
        "context_capital_forward_evaluation",
        content_hash(spec),
        published,
        source_hash,
        paired,
        incomplete_forecast_ids,
        outcome,
        reason_codes,
        ContextCapitalForwardResult.model_fields["limitations"].default,
    )
    return ContextCapitalForwardResult(
        result_id=result_id,
        evaluation_spec_hash=content_hash(spec),
        plan_id=spec.plan_id,
        published_at=published,
        source_hash=source_hash,
        opportunities=paired,
        incomplete_forecast_ids=incomplete_forecast_ids,
        natural_opportunity_count=len(paired) + len(incomplete_forecast_ids),
        paired_opportunity_count=len(paired),
        veto_count=sum(_is_entry_veto(item.context_status) for item in paired),
        fallback_count=sum(item.used_program_fallback for item in paired),
        base_average_net_return_bps=base_mean,
        context_average_net_return_bps=context_mean,
        average_return_delta_bps=delta_mean,
        return_delta_lower_bound_bps=delta_lower,
        outcome=outcome,
        reason_codes=reason_codes,
    )


def _capital_objective_id(assessment: ContextAssessment) -> str | None:
    if assessment.capital_implication is not None:
        return assessment.capital_implication.objective_id
    if assessment.capital_relevance is not None:
        return assessment.capital_relevance.objective_id
    return None


def _capital_status(
    assessment: ContextAssessment | None,
) -> ContextCapitalRelevanceStatus | ContextCapitalEffect | None:
    if assessment is None:
        return None
    if assessment.capital_implication is not None:
        return assessment.capital_implication.effect
    if assessment.capital_relevance is not None:
        return assessment.capital_relevance.status
    return None


def _is_entry_veto(
    status: ContextCapitalRelevanceStatus | ContextCapitalEffect | None,
) -> bool:
    return status in {
        ContextCapitalRelevanceStatus.ENTRY_VETO_CANDIDATE,
        ContextCapitalEffect.OPPOSE,
    }


def failed_context_capital_experiment(
    result: ContextCapitalForwardResult,
    *,
    rejected_at: datetime,
) -> FailedExperiment:
    if result.outcome != ContextCapitalForwardOutcome.FAILED:
        raise ValueError("只有样本充分且未通过的 Context Capital 结果可登记失败")
    hypothesis = (
        f"Context 行为 {result.plan_id} 对程序候选入场的确定性否决，"
        "能产生为正的费用后配对收益增量保守下界"
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_context_capital_forward", result.result_id),
        hypothesis_fingerprint=content_hash({"hypothesis": hypothesis.strip().lower()}),
        evidence_ids=(f"hypothesis:{hypothesis}", result.result_id),
        rejected_at=require_utc(rejected_at),
        reason_codes=("CONTEXT_CAPITAL_FORWARD_FAILED", *result.reason_codes),
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _lower_bound(values: tuple[Decimal, ...], z: Decimal) -> Decimal | None:
    mean = _mean(values)
    if mean is None or len(values) < 2:
        return None
    variance = sum(
        ((item - mean) ** 2 for item in values),
        Decimal("0"),
    ) / Decimal(len(values) - 1)
    standard_error = (variance / Decimal(len(values))).sqrt()
    return mean - z * standard_error
