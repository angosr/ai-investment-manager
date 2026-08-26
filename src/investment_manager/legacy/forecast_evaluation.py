from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.models import AssessmentOutcomeStatus, DirectionalView
from investment_manager.forecast.tables import codex_runs
from investment_manager.governance.models import EvaluationPlan, EvaluationStage, FailedExperiment
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.legacy.models import (
    AnalysisForecastOutcome,
    AnalysisProposal,
    DirectionalForecast,
)
from investment_manager.legacy.tables import (
    analysis_cycles,
    analysis_forecast_outcomes,
    analysis_proposals,
    market_snapshots,
)
from investment_manager.market.repository import trade_at_or_before
from investment_manager.platform.time import database_utc


@dataclass(frozen=True, slots=True)
class PendingAnalysisForecast:
    proposal: AnalysisProposal
    forecast: DirectionalForecast
    cycle_id: str
    pipeline_version: str
    analysis_behavior_hash: str | None
    analysis_as_of: datetime
    available_at: datetime | None
    source_run_id: str | None
    frozen_reference_price: Decimal


@dataclass(frozen=True, slots=True)
class ForecastSettlementResult:
    settled: int = 0
    abstained: int = 0
    unscorable: int = 0
    pending: int = 0


class ForecastScopeMetrics(FrozenModel):
    symbol: str
    view_horizon_minutes: int = Field(gt=0)
    outcome_count: int = Field(ge=0)
    scored_count: int = Field(ge=0)
    abstained_count: int = Field(ge=0)
    unscorable_count: int = Field(ge=0)
    non_overlapping_scored_count: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    directional_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    average_directional_return_bps: Decimal | None = None
    average_directional_return_bps_lower_bound: Decimal | None = None
    baseline_id: str | None = None
    always_up_correct_count: int | None = Field(default=None, ge=0)
    always_up_directional_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    always_up_average_return_bps: Decimal | None = None
    directional_accuracy_delta_vs_always_up: Decimal | None = None
    average_directional_return_bps_delta_vs_always_up: Decimal | None = None
    average_directional_return_bps_delta_lower_bound_vs_always_up: Decimal | None = None
    sample_sufficient: bool


class ForecastEvaluationReport(FrozenModel):
    report_id: str
    report_version: str
    outcome_evaluation_version: str
    pipeline_version: str | None = None
    analysis_behavior_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    source_pipeline_versions: tuple[str, ...] = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    published_at: datetime
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: tuple[ForecastScopeMetrics, ...] = Field(min_length=1)
    statistically_conclusive: bool
    limitations: tuple[str, ...]
    outcome_ids: tuple[str, ...]

    _utc_window_start = field_validator("window_start")(require_utc)
    _utc_window_end = field_validator("window_end")(require_utc)
    _utc_published_at = field_validator("published_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_window_match(self):
        if not self.window_start < self.window_end <= self.published_at:
            raise ValueError("预测评价窗口和发布时间顺序非法")
        if (self.pipeline_version is None) == (self.analysis_behavior_hash is None):
            raise ValueError("预测评价必须且只能选择 Pipeline 或分析行为作用域")
        if tuple(sorted(set(self.source_pipeline_versions))) != (self.source_pipeline_versions):
            raise ValueError("预测评价来源 Pipeline 必须唯一且有序")
        if self.pipeline_version is not None and self.source_pipeline_versions != (
            self.pipeline_version,
        ):
            raise ValueError("Pipeline 评价不得混入其他运行代次")
        scope = {
            "pipeline_version": self.pipeline_version,
            "analysis_behavior_hash": self.analysis_behavior_hash,
        }
        expected_id = stable_id(
            "forecast_evaluation_report",
            self.report_version,
            self.outcome_evaluation_version,
            scope,
            self.window_start,
            self.window_end,
            self.published_at,
            self.source_hash,
        )
        if self.report_id != expected_id:
            raise ValueError("预测评价 report_id 不一致")
        return self


class ForwardForecastEvaluationSpec(FrozenModel):
    """Result-before-known contract for one behavior-equivalent forecast cohort."""

    version: Literal["forward-forecast-evaluation-spec-v1"] = "forward-forecast-evaluation-spec-v1"
    plan_id: str
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_evaluation_version: str
    signal_window_start: datetime
    signal_window_end: datetime
    symbols: tuple[str, ...] = Field(min_length=1)
    horizons_minutes: tuple[int, ...] = Field(min_length=1)
    minimum_non_overlapping_samples: int = Field(default=30, ge=2)
    settlement_grace_minutes: int = Field(default=120, ge=0, le=1440)
    report_version: Literal["analysis-forecast-report-v3"] = "analysis-forecast-report-v3"
    lower_confidence_z: Decimal = Field(default=Decimal("1.96"), gt=0)
    baseline_id: Literal["always-up-on-ai-scored-timestamps-v1"] = (
        "always-up-on-ai-scored-timestamps-v1"
    )

    _utc_signal_start = field_validator("signal_window_start")(require_utc)
    _utc_signal_end = field_validator("signal_window_end")(require_utc)

    @model_validator(mode="after")
    def scope_is_canonical_and_feasible(self):
        if not self.signal_window_start < self.signal_window_end:
            raise ValueError("前向预测信号窗口起点必须早于终点")
        if self.symbols != tuple(sorted(set(self.symbols))):
            raise ValueError("前向预测品种必须唯一且有序")
        if self.horizons_minutes != tuple(sorted(set(self.horizons_minutes))) or any(
            item <= 0 for item in self.horizons_minutes
        ):
            raise ValueError("前向预测周期必须为唯一、升序正整数")
        required_span = timedelta(
            minutes=max(self.horizons_minutes) * self.minimum_non_overlapping_samples
        )
        if self.signal_window_end - self.signal_window_start < required_span:
            raise ValueError("前向预测窗口不足以形成预登记非重叠样本")
        return self


class ForwardForecastEvaluationResult(FrozenModel):
    version: Literal["forward-forecast-evaluation-result-v1"] = (
        "forward-forecast-evaluation-result-v1"
    )
    result_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str
    expected_scopes: tuple[tuple[str, int], ...] = Field(min_length=1)
    report: ForecastEvaluationReport
    passed_incremental_gate: bool
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def identity_matches_payload(self):
        expected = stable_id(
            "forward_forecast_evaluation",
            self.evaluation_spec_hash,
            self.report.report_id,
            self.expected_scopes,
            self.passed_incremental_gate,
            self.reason_codes,
        )
        if self.result_id != expected:
            raise ValueError("前向预测评价结果身份不一致")
        return self


class _ForwardForecastEvaluationEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: ForwardForecastEvaluationResult


class ForwardForecastEvaluationCatalog:
    """Content-addressed, immutable storage for one matured forward result."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: ForwardForecastEvaluationResult) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一前向预测结果 ID 的内容不一致")
            return target
        envelope = _ForwardForecastEvaluationEnvelope(
            result_hash=content_hash(result),
            result=result,
        )
        self._root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".forward-forecast-",
            suffix=".json",
            dir=self._root,
            delete=False,
        ) as temporary:
            temporary.write(canonical_json(envelope))
            temporary.flush()
            temporary_path = Path(temporary.name)
        try:
            temporary_path.replace(target)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return target

    def load(self, result_id: str) -> ForwardForecastEvaluationResult:
        raw = json.loads((self._root / f"{result_id}.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(raw.get("result")):
            raise ValueError("前向预测评价制品内容哈希不匹配")
        envelope = _ForwardForecastEvaluationEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("前向预测评价文件名与结果 ID 不一致")
        return envelope.result


def failed_forward_forecast_experiment(
    result: ForwardForecastEvaluationResult,
    *,
    rejected_at: datetime,
) -> FailedExperiment:
    if result.passed_incremental_gate:
        raise ValueError("通过的前向预测评价不能登记为失败实验")
    hypothesis = (
        f"AI 行为 {result.report.analysis_behavior_hash} 在预登记计划 "
        f"{result.plan_id} 的全部品种与周期上，相对 always-UP 的配对收益增量"
        "保守下界为正且每个作用域样本充分"
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_forward_forecast", result.result_id),
        hypothesis_fingerprint=content_hash({"hypothesis": hypothesis.strip().lower()}),
        evidence_ids=(f"hypothesis:{hypothesis}", result.result_id),
        rejected_at=require_utc(rejected_at),
        reason_codes=("FORWARD_FORECAST_FAILED", *result.reason_codes),
    )


def build_forward_forecast_evaluation_plan(
    *,
    spec: ForwardForecastEvaluationSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    registered = require_utc(registered_at)
    if registered >= spec.signal_window_start:
        raise ValueError("前向预测计划必须在首个信号生成前登记")
    return EvaluationPlan(
        plan_id=spec.plan_id,
        registered_at=registered,
        base_manifest_id=base_manifest_id,
        primary_metric=("average_directional_return_bps_delta_lower_bound_vs_always_up"),
        minimum_sample_size=(
            spec.minimum_non_overlapping_samples * len(spec.symbols) * len(spec.horizons_minutes)
        ),
        hard_guardrails=(
            "NON_OVERLAPPING_SAMPLE_SUFFICIENT_EACH_SCOPE",
            "PAIRED_RETURN_DELTA_LOWER_BOUND_POSITIVE_EACH_SCOPE",
            "NO_OVERDUE_FORECAST_IN_SIGNAL_WINDOW",
        ),
        required_stages=(EvaluationStage.SHADOW,),
        fixed_regression_suite_version="analysis-forecast-evaluation-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
    )


def validate_forward_forecast_evaluation_plan(
    *,
    spec: ForwardForecastEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
    published_at: datetime,
) -> None:
    published = require_utc(published_at)
    if plan.plan_id != spec.plan_id or plan.base_manifest_id != champion_manifest_id:
        raise ValueError("前向预测计划身份或治理基线不一致")
    if plan.registered_at >= spec.signal_window_start:
        raise ValueError("前向预测计划未在首个信号前登记")
    if plan.candidate_spec_hash != content_hash(spec):
        raise ValueError("前向预测行为、窗口、作用域或门槛与预登记规格不一致")
    expected_size = (
        spec.minimum_non_overlapping_samples * len(spec.symbols) * len(spec.horizons_minutes)
    )
    if (
        plan.minimum_sample_size != expected_size
        or plan.required_stages != (EvaluationStage.SHADOW,)
        or plan.blind_query_budget != 0
    ):
        raise ValueError("前向预测计划阶段或样本门槛不一致")
    complete_after = spec.signal_window_end + timedelta(
        minutes=max(spec.horizons_minutes) + spec.settlement_grace_minutes
    )
    if published < complete_after:
        raise ValueError("前向预测窗口尚未完整到期并经过结算宽限")


class SqlAnalysisForecastOutcomeStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def pending(
        self,
        *,
        limit: int,
    ) -> tuple[PendingAnalysisForecast, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("方向预测结算批次必须在 1..1000")
        with self._engine.connect() as connection:
            outcome_counts = (
                select(
                    analysis_forecast_outcomes.c.proposal_id,
                    func.count(analysis_forecast_outcomes.c.outcome_id).label("outcome_count"),
                )
                .group_by(analysis_forecast_outcomes.c.proposal_id)
                .subquery()
            )
            rows = connection.execute(
                select(
                    analysis_proposals.c.payload.label("proposal"),
                    analysis_proposals.c.forecast_count,
                    analysis_cycles.c.cycle_id,
                    analysis_cycles.c.pipeline_version,
                    analysis_cycles.c.as_of,
                    market_snapshots.c.symbol,
                    market_snapshots.c.payload.label("market"),
                )
                .join(
                    analysis_cycles,
                    analysis_cycles.c.cycle_id == analysis_proposals.c.cycle_id,
                )
                .join(
                    market_snapshots,
                    market_snapshots.c.cycle_id == analysis_proposals.c.cycle_id,
                )
                .outerjoin(
                    outcome_counts,
                    outcome_counts.c.proposal_id == analysis_proposals.c.proposal_id,
                )
                .where(
                    func.coalesce(outcome_counts.c.outcome_count, 0)
                    < analysis_proposals.c.forecast_count,
                )
                .order_by(analysis_cycles.c.as_of, analysis_proposals.c.proposal_id)
                .limit(limit)
            ).mappings()

            proposal_rows = tuple(rows)
            proposal_ids = tuple(
                AnalysisProposal.model_validate(row["proposal"]).proposal_id
                for row in proposal_rows
            )
            existing_keys = (
                set(
                    connection.execute(
                        select(
                            analysis_forecast_outcomes.c.proposal_id,
                            analysis_forecast_outcomes.c.view_horizon_minutes,
                        ).where(analysis_forecast_outcomes.c.proposal_id.in_(proposal_ids))
                    ).tuples()
                )
                if proposal_ids
                else set()
            )
            cycle_ids = tuple(str(row["cycle_id"]) for row in proposal_rows)
            run_rows = (
                tuple(
                    connection.execute(
                        select(
                            codex_runs.c.run_id,
                            codex_runs.c.cycle_id,
                            codex_runs.c.status,
                            codex_runs.c.payload,
                        ).where(codex_runs.c.cycle_id.in_(cycle_ids))
                    ).mappings()
                )
                if cycle_ids
                else ()
            )
            runs_by_cycle: dict[str, list[dict]] = {}
            for run in run_rows:
                runs_by_cycle.setdefault(str(run["cycle_id"]), []).append(dict(run))

            pending: list[PendingAnalysisForecast] = []
            for row in proposal_rows:
                proposal = AnalysisProposal.model_validate(row["proposal"])
                if int(row["forecast_count"]) != len(proposal.forecasts):
                    raise ValueError("Proposal 预测数量投影与冻结 Payload 不一致")
                if proposal.symbol != row["symbol"]:
                    raise ValueError("方向预测 Proposal 与 MarketSnapshot 品种不一致")
                market_payload = row["market"]
                if not isinstance(market_payload, dict) or "last" not in market_payload:
                    raise ValueError("方向预测缺少冻结参考价格")
                reference_price = Decimal(str(market_payload["last"]))
                if reference_price <= 0:
                    raise ValueError("方向预测冻结参考价格必须为正")
                cycle_id = str(row["cycle_id"])
                analysis_as_of = database_utc(row["as_of"])
                (
                    available_at,
                    source_run_id,
                    analysis_behavior_hash,
                ) = unique_successful_codex_completion(
                    runs_by_cycle.get(cycle_id, []),
                    analysis_as_of=analysis_as_of,
                )
                for forecast in proposal.forecasts:
                    if (proposal.proposal_id, forecast.horizon_minutes) in existing_keys:
                        continue
                    pending.append(
                        PendingAnalysisForecast(
                            proposal=proposal,
                            forecast=forecast,
                            cycle_id=cycle_id,
                            pipeline_version=str(row["pipeline_version"]),
                            analysis_behavior_hash=analysis_behavior_hash,
                            analysis_as_of=analysis_as_of,
                            available_at=available_at,
                            source_run_id=source_run_id,
                            frozen_reference_price=reference_price,
                        )
                    )
                    if len(pending) >= limit:
                        return tuple(pending)
        return tuple(pending)

    def record(self, outcome: AnalysisForecastOutcome) -> bool:
        payload = outcome.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(analysis_forecast_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        proposal_id=outcome.proposal_id,
                        cycle_id=outcome.cycle_id,
                        pipeline_version=outcome.pipeline_version,
                        analysis_behavior_hash=outcome.analysis_behavior_hash,
                        view_horizon_minutes=outcome.view_horizon_minutes,
                        status=outcome.status.value,
                        evaluation_at=outcome.evaluation_at,
                        settled_at=outcome.settled_at,
                        directional_return_bps=outcome.directional_return_bps,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(analysis_forecast_outcomes.c.payload).where(
                        analysis_forecast_outcomes.c.proposal_id == outcome.proposal_id,
                        analysis_forecast_outcomes.c.view_horizon_minutes
                        == outcome.view_horizon_minutes,
                    )
                ).scalar_one_or_none()
            if existing is None or AnalysisForecastOutcome.model_validate(existing) != outcome:
                raise ValueError("AnalysisForecastOutcome 已存在且内容不同") from None
            return False

    def visible_outcomes(
        self,
        *,
        pipeline_version: str | None = None,
        analysis_behavior_hash: str | None = None,
        window_start: datetime,
        window_end: datetime,
        published_at: datetime,
    ) -> tuple[AnalysisForecastOutcome, ...]:
        start = require_utc(window_start)
        end = require_utc(window_end)
        published = require_utc(published_at)
        if not start < end <= published:
            raise ValueError("预测评价窗口和发布时间顺序非法")
        if (pipeline_version is None) == (analysis_behavior_hash is None):
            raise ValueError("预测评价必须且只能选择 Pipeline 或分析行为作用域")
        scope_filter = (
            analysis_forecast_outcomes.c.pipeline_version == pipeline_version
            if pipeline_version is not None
            else analysis_forecast_outcomes.c.analysis_behavior_hash == analysis_behavior_hash
        )
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(analysis_forecast_outcomes.c.payload)
                .where(
                    scope_filter,
                    analysis_forecast_outcomes.c.evaluation_at >= start,
                    analysis_forecast_outcomes.c.evaluation_at < end,
                    analysis_forecast_outcomes.c.settled_at <= published,
                )
                .order_by(
                    analysis_forecast_outcomes.c.evaluation_at,
                    analysis_forecast_outcomes.c.outcome_id,
                )
            ).scalars()
            return tuple(AnalysisForecastOutcome.model_validate(payload) for payload in payloads)

    def visible_outcomes_for_signal_window(
        self,
        *,
        spec: ForwardForecastEvaluationSpec,
        published_at: datetime,
    ) -> tuple[AnalysisForecastOutcome, ...]:
        """Select exact Codex-completion times without widening horizon boundaries."""

        published = require_utc(published_at)
        horizon_ranges = tuple(
            and_(
                analysis_forecast_outcomes.c.view_horizon_minutes == horizon,
                analysis_forecast_outcomes.c.evaluation_at
                >= spec.signal_window_start + timedelta(minutes=horizon),
                analysis_forecast_outcomes.c.evaluation_at
                < spec.signal_window_end + timedelta(minutes=horizon),
            )
            for horizon in spec.horizons_minutes
        )
        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(analysis_forecast_outcomes.c.payload)
                    .where(
                        analysis_forecast_outcomes.c.analysis_behavior_hash
                        == spec.analysis_behavior_hash,
                        or_(*horizon_ranges),
                        analysis_forecast_outcomes.c.settled_at <= published,
                    )
                    .order_by(
                        analysis_forecast_outcomes.c.evaluation_at,
                        analysis_forecast_outcomes.c.outcome_id,
                    )
                ).scalars()
            )
        outcomes = tuple(AnalysisForecastOutcome.model_validate(payload) for payload in payloads)
        if any(
            item.symbol not in spec.symbols
            or item.view_horizon_minutes not in spec.horizons_minutes
            or item.analysis_behavior_hash != spec.analysis_behavior_hash
            or not spec.signal_window_start <= item.signal_observed_at < spec.signal_window_end
            for item in outcomes
        ):
            raise ValueError("前向预测查询混入预登记 signal-time 作用域外结果")
        return outcomes


@dataclass(frozen=True, slots=True)
class AnalysisForecastEvaluator:
    report_version: str = "analysis-forecast-report-v3"
    minimum_non_overlapping_samples: int = 30
    lower_confidence_z: Decimal = Decimal("1.96")

    def evaluate(
        self,
        *,
        outcomes: tuple[AnalysisForecastOutcome, ...],
        outcome_evaluation_version: str,
        pipeline_version: str | None = None,
        analysis_behavior_hash: str | None = None,
        window_start: datetime,
        window_end: datetime,
        published_at: datetime,
    ) -> ForecastEvaluationReport:
        start = require_utc(window_start)
        end = require_utc(window_end)
        published = require_utc(published_at)
        if not start < end <= published:
            raise ValueError("预测评价窗口和发布时间顺序非法")
        if (pipeline_version is None) == (analysis_behavior_hash is None):
            raise ValueError("预测评价必须且只能选择 Pipeline 或分析行为作用域")
        if not outcomes:
            raise ValueError("预测评价至少需要一个到期结果")
        if self.minimum_non_overlapping_samples < 2:
            raise ValueError("预测评价最小非重叠样本数至少为 2")
        if self.lower_confidence_z <= 0:
            raise ValueError("预测评价置信下界 z 必须为正")
        ids = [item.outcome_id for item in outcomes]
        proposal_horizons = [(item.proposal_id, item.view_horizon_minutes) for item in outcomes]
        if len(ids) != len(set(ids)) or len(proposal_horizons) != len(set(proposal_horizons)):
            raise ValueError("预测评价结果或 Proposal 周期不得重复")
        if any(
            (
                item.pipeline_version != pipeline_version
                if pipeline_version is not None
                else item.analysis_behavior_hash != analysis_behavior_hash
            )
            or item.evaluation_version != outcome_evaluation_version
            or not start <= item.evaluation_at < end
            or item.settled_at > published
            for item in outcomes
        ):
            raise ValueError("预测评价包含作用域外或发布时间后才可见的结果")

        ordered = tuple(sorted(outcomes, key=lambda item: (item.evaluation_at, item.outcome_id)))
        source_pipeline_versions = tuple(sorted({item.pipeline_version for item in ordered}))
        scopes: list[ForecastScopeMetrics] = []
        scope_keys = sorted({(item.symbol, item.view_horizon_minutes) for item in ordered})
        for symbol, horizon in scope_keys:
            scoped = tuple(
                item
                for item in ordered
                if item.symbol == symbol and item.view_horizon_minutes == horizon
            )
            scored = tuple(
                item for item in scoped if item.status == AssessmentOutcomeStatus.SETTLED
            )
            independent = _non_overlapping(scored)
            returns = tuple(
                item.directional_return_bps
                for item in independent
                if item.directional_return_bps is not None
            )
            market_returns = tuple(
                item.market_return_bps for item in independent if item.market_return_bps is not None
            )
            if len(returns) != len(independent) or len(market_returns) != len(independent):
                raise ValueError("SETTLED 方向预测缺少可比收益事实")
            correct = sum(item.direction_correct is True for item in independent)
            always_up_correct = sum(item > 0 for item in market_returns)
            directional_accuracy = (
                Decimal(correct) / Decimal(len(independent)) if independent else None
            )
            always_up_accuracy = (
                Decimal(always_up_correct) / Decimal(len(independent)) if independent else None
            )
            average_return = sum(returns, Decimal("0")) / Decimal(len(returns)) if returns else None
            always_up_average = (
                sum(market_returns, Decimal("0")) / Decimal(len(market_returns))
                if market_returns
                else None
            )
            paired_return_deltas = tuple(
                directional - baseline
                for directional, baseline in zip(returns, market_returns, strict=True)
            )
            sample_sufficient = len(independent) >= self.minimum_non_overlapping_samples
            scopes.append(
                ForecastScopeMetrics(
                    symbol=symbol,
                    view_horizon_minutes=horizon,
                    outcome_count=len(scoped),
                    scored_count=len(scored),
                    abstained_count=sum(
                        item.status == AssessmentOutcomeStatus.ABSTAINED for item in scoped
                    ),
                    unscorable_count=sum(
                        item.status == AssessmentOutcomeStatus.UNSCORABLE for item in scoped
                    ),
                    non_overlapping_scored_count=len(independent),
                    correct_count=correct,
                    directional_accuracy=directional_accuracy,
                    average_directional_return_bps=average_return,
                    average_directional_return_bps_lower_bound=_mean_lower_bound(
                        returns,
                        z=self.lower_confidence_z,
                    ),
                    baseline_id="always-up-on-ai-scored-timestamps-v1",
                    always_up_correct_count=always_up_correct,
                    always_up_directional_accuracy=always_up_accuracy,
                    always_up_average_return_bps=always_up_average,
                    directional_accuracy_delta_vs_always_up=(
                        directional_accuracy - always_up_accuracy
                        if directional_accuracy is not None and always_up_accuracy is not None
                        else None
                    ),
                    average_directional_return_bps_delta_vs_always_up=(
                        average_return - always_up_average
                        if average_return is not None and always_up_average is not None
                        else None
                    ),
                    average_directional_return_bps_delta_lower_bound_vs_always_up=(
                        _mean_lower_bound(
                            paired_return_deltas,
                            z=self.lower_confidence_z,
                        )
                    ),
                    sample_sufficient=sample_sufficient,
                )
            )
        conclusive = all(item.sample_sufficient for item in scopes)
        limitations = ["NON_TRADABLE_DIRECTIONAL_FORECAST_ONLY"]
        if not conclusive:
            limitations.append("NON_OVERLAPPING_SAMPLE_TOO_SMALL")
        source_hash = content_hash([item.model_dump(mode="json") for item in ordered])
        scope = {
            "pipeline_version": pipeline_version,
            "analysis_behavior_hash": analysis_behavior_hash,
        }
        return ForecastEvaluationReport(
            report_id=stable_id(
                "forecast_evaluation_report",
                self.report_version,
                outcome_evaluation_version,
                scope,
                start,
                end,
                published,
                source_hash,
            ),
            report_version=self.report_version,
            outcome_evaluation_version=outcome_evaluation_version,
            pipeline_version=pipeline_version,
            analysis_behavior_hash=analysis_behavior_hash,
            source_pipeline_versions=source_pipeline_versions,
            window_start=start,
            window_end=end,
            published_at=published,
            source_hash=source_hash,
            scopes=tuple(scopes),
            statistically_conclusive=conclusive,
            limitations=tuple(limitations),
            outcome_ids=tuple(item.outcome_id for item in ordered),
        )


def evaluate_forward_forecast_plan(
    *,
    spec: ForwardForecastEvaluationSpec,
    outcomes: tuple[AnalysisForecastOutcome, ...],
    published_at: datetime,
) -> ForwardForecastEvaluationResult:
    """Evaluate only the cohort frozen by a result-before-known signal window."""

    published = require_utc(published_at)
    if any(
        item.analysis_behavior_hash != spec.analysis_behavior_hash
        or item.symbol not in spec.symbols
        or item.view_horizon_minutes not in spec.horizons_minutes
        or not spec.signal_window_start <= item.signal_observed_at < spec.signal_window_end
        for item in outcomes
    ):
        raise ValueError("前向预测评价包含预登记 signal-time 作用域外结果")
    report = AnalysisForecastEvaluator(
        report_version=spec.report_version,
        minimum_non_overlapping_samples=spec.minimum_non_overlapping_samples,
        lower_confidence_z=spec.lower_confidence_z,
    ).evaluate(
        outcomes=outcomes,
        outcome_evaluation_version=spec.outcome_evaluation_version,
        analysis_behavior_hash=spec.analysis_behavior_hash,
        window_start=spec.signal_window_start + timedelta(minutes=min(spec.horizons_minutes)),
        window_end=spec.signal_window_end + timedelta(minutes=max(spec.horizons_minutes)),
        published_at=published,
    )
    expected_scopes = tuple(
        (symbol, horizon) for symbol in spec.symbols for horizon in spec.horizons_minutes
    )
    observed_scopes = tuple((scope.symbol, scope.view_horizon_minutes) for scope in report.scopes)
    reasons: list[str] = []
    if observed_scopes != expected_scopes:
        reasons.append("EXPECTED_SCOPE_MISSING")
    if any(not scope.sample_sufficient for scope in report.scopes):
        reasons.append("NON_OVERLAPPING_SAMPLE_TOO_SMALL")
    if any(
        scope.baseline_id != spec.baseline_id
        or scope.average_directional_return_bps_delta_lower_bound_vs_always_up is None
        or scope.average_directional_return_bps_delta_lower_bound_vs_always_up <= 0
        for scope in report.scopes
    ):
        reasons.append("PAIRED_RETURN_DELTA_LOWER_BOUND_NOT_POSITIVE")
    spec_hash = content_hash(spec)
    reason_codes = tuple(reasons)
    passed = not reasons
    result_id = stable_id(
        "forward_forecast_evaluation",
        spec_hash,
        report.report_id,
        expected_scopes,
        passed,
        reason_codes,
    )
    return ForwardForecastEvaluationResult(
        result_id=result_id,
        evaluation_spec_hash=spec_hash,
        plan_id=spec.plan_id,
        expected_scopes=expected_scopes,
        report=report,
        passed_incremental_gate=passed,
        reason_codes=reason_codes,
    )


def pending_forecast_matches_forward_spec(
    pending: PendingAnalysisForecast,
    spec: ForwardForecastEvaluationSpec,
) -> bool:
    return (
        pending.available_at is not None
        and pending.analysis_behavior_hash == spec.analysis_behavior_hash
        and pending.proposal.symbol in spec.symbols
        and pending.forecast.horizon_minutes in spec.horizons_minutes
        and spec.signal_window_start <= pending.available_at < spec.signal_window_end
    )


@dataclass(slots=True)
class AnalysisForecastOutcomeSettler:
    engine: Engine
    store: SqlAnalysisForecastOutcomeStore
    evaluation_version: str
    maximum_market_age_seconds: int
    settlement_grace_minutes: int
    batch_size: int = 100

    def settle(self, *, as_of: datetime) -> ForecastSettlementResult:
        now = require_utc(as_of)
        settled = abstained = unscorable = pending = 0
        forecasts = self.store.pending(
            limit=self.batch_size,
        )
        for forecast in forecasts:
            proposal = forecast.proposal
            directional = forecast.forecast
            signal_at = forecast.available_at or forecast.analysis_as_of
            evaluation_at = signal_at + timedelta(minutes=directional.horizon_minutes)
            if evaluation_at > now:
                pending += 1
                continue
            common = {
                "outcome_id": stable_id(
                    "analysis_forecast_outcome",
                    proposal.proposal_id,
                    directional.horizon_minutes,
                    self.evaluation_version,
                ),
                "proposal_id": proposal.proposal_id,
                "cycle_id": forecast.cycle_id,
                "pipeline_version": forecast.pipeline_version,
                "analysis_behavior_hash": forecast.analysis_behavior_hash,
                "evaluation_version": self.evaluation_version,
                "symbol": proposal.symbol,
                "directional_view": directional.directional_view,
                "confidence": directional.confidence,
                "view_horizon_minutes": directional.horizon_minutes,
                "signal_observed_at": signal_at,
                "evaluation_at": evaluation_at,
                "settled_at": now,
                "reference_price": forecast.frozen_reference_price,
            }
            if forecast.available_at is None or forecast.source_run_id is None:
                outcome = AnalysisForecastOutcome(
                    **common,
                    status=AssessmentOutcomeStatus.UNSCORABLE,
                    reason_code="CODEX_COMPLETION_TIME_MISSING_OR_AMBIGUOUS",
                )
                unscorable += int(self.store.record(outcome))
                continue
            reference_trade = trade_at_or_before(
                self.engine,
                symbol=proposal.symbol,
                evaluation_at=forecast.available_at,
                visible_at=forecast.available_at,
            )
            fresh_reference = reference_trade is not None and timedelta(
                0
            ) <= forecast.available_at - reference_trade.event_time <= timedelta(
                seconds=self.maximum_market_age_seconds
            )
            if not fresh_reference:
                outcome = AnalysisForecastOutcome(
                    **common,
                    status=AssessmentOutcomeStatus.UNSCORABLE,
                    reason_code="REFERENCE_MARKET_DATA_MISSING_AT_FORECAST_AVAILABILITY",
                )
                unscorable += int(self.store.record(outcome))
                continue
            assert reference_trade is not None
            common["reference_price"] = reference_trade.price
            trade = trade_at_or_before(
                self.engine,
                symbol=proposal.symbol,
                evaluation_at=evaluation_at,
                visible_at=now,
            )
            fresh_trade = trade is not None and timedelta(
                0
            ) <= evaluation_at - trade.event_time <= timedelta(
                seconds=self.maximum_market_age_seconds
            )
            if fresh_trade:
                assert trade is not None
                market_return = (trade.price / reference_trade.price - Decimal("1")) * Decimal(
                    "10000"
                )
                if directional.directional_view == DirectionalView.UNCERTAIN:
                    outcome = AnalysisForecastOutcome(
                        **common,
                        status=AssessmentOutcomeStatus.ABSTAINED,
                        exit_price=trade.price,
                        exit_event_time=trade.event_time,
                        market_return_bps=market_return,
                        reason_code="DIRECTIONAL_VIEW_ABSTAINED",
                    )
                    abstained += int(self.store.record(outcome))
                    continue
                directional_return = (
                    market_return
                    if directional.directional_view == DirectionalView.UP
                    else -market_return
                )
                outcome = AnalysisForecastOutcome(
                    **common,
                    status=AssessmentOutcomeStatus.SETTLED,
                    exit_price=trade.price,
                    exit_event_time=trade.event_time,
                    market_return_bps=market_return,
                    directional_return_bps=directional_return,
                    direction_correct=directional_return > 0,
                    reason_code="DIRECTIONAL_RETURN_AVAILABLE",
                )
                settled += int(self.store.record(outcome))
                continue
            if now - evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                pending += 1
                continue
            outcome = AnalysisForecastOutcome(
                **common,
                status=AssessmentOutcomeStatus.UNSCORABLE,
                reason_code="MARKET_DATA_MISSING_AT_FORECAST_HORIZON",
            )
            unscorable += int(self.store.record(outcome))
        return ForecastSettlementResult(
            settled=settled,
            abstained=abstained,
            unscorable=unscorable,
            pending=pending,
        )


def unique_successful_codex_completion(
    rows: list[dict],
    *,
    analysis_as_of: datetime,
) -> tuple[datetime | None, str | None, str | None]:
    successful = [row for row in rows if row.get("status") == "SUCCEEDED"]
    if len(successful) != 1:
        return None, None, None
    row = successful[0]
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None, None, None
    raw_completed_at = payload.get("completed_at")
    if not isinstance(raw_completed_at, str):
        return None, None, None
    try:
        completed_at = database_utc(datetime.fromisoformat(raw_completed_at))
    except ValueError:
        return None, None, None
    if completed_at < analysis_as_of:
        return None, None, None
    run_id = row.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None, None, None
    raw_behavior_hash = payload.get("analysis_behavior_hash")
    behavior_hash = (
        raw_behavior_hash
        if isinstance(raw_behavior_hash, str)
        and len(raw_behavior_hash) == 64
        and all(character in "0123456789abcdef" for character in raw_behavior_hash)
        else None
    )
    return completed_at, run_id, behavior_hash


def _non_overlapping(
    outcomes: tuple[AnalysisForecastOutcome, ...],
) -> tuple[AnalysisForecastOutcome, ...]:
    selected: list[AnalysisForecastOutcome] = []
    last_evaluation_at: datetime | None = None
    for outcome in sorted(
        outcomes,
        key=lambda item: (item.signal_observed_at, item.evaluation_at, item.outcome_id),
    ):
        if last_evaluation_at is not None and outcome.signal_observed_at < last_evaluation_at:
            continue
        selected.append(outcome)
        last_evaluation_at = outcome.evaluation_at
    return tuple(selected)


def _mean_lower_bound(
    values: tuple[Decimal, ...],
    *,
    z: Decimal,
) -> Decimal | None:
    if len(values) < 2:
        return None
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    variance = sum(((item - mean) ** 2 for item in values), Decimal("0")) / (count - 1)
    return mean - z * (variance / count).sqrt()
