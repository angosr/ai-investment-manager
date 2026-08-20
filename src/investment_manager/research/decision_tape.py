from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine

from investment_manager.analyst import ANALYST_INPUT_VERSION
from investment_manager.execution.models import (
    AccountSnapshot,
    Side,
)
from investment_manager.forecast.models import DirectionalView
from investment_manager.forecast.tables import codex_runs
from investment_manager.forecast_evaluation import unique_successful_codex_completion
from investment_manager.governance.models import EvaluationPlan, EvaluationStage
from investment_manager.information.models import IntelligenceEvent
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.legacy.models import (
    AnalysisProposal,
    DirectionalForecast,
    SignalCandidate,
)
from investment_manager.legacy.repository import (
    analysis_cycles,
    analysis_proposals,
    market_snapshots,
)
from investment_manager.market.models import (
    FeatureSnapshot,
    MarketSnapshot,
)
from investment_manager.platform.time import database_utc
from investment_manager.research.backtest import (
    BacktestRun,
    ResearchStrategy,
    artifact_hash,
    run_bar_backtest,
)
from investment_manager.research.dataset import HistoricalDataset
from investment_manager.research.walk_forward import BlindEvaluationResult
from investment_manager.settings import AppConfig

FORECAST_DECISION_TAPE_VERSION = "forecast-decision-tape-v1"
PAIRED_DECISION_TAPE_EVALUATION_VERSION = "paired-decision-tape-evaluation-v4"


class ForecastTapeEntry(FrozenModel):
    entry_id: str
    proposal_id: str
    cycle_id: str
    pipeline_version: str
    source_run_id: str
    symbol: str
    available_at: datetime
    horizon_minutes: int = Field(gt=0)
    directional_view: DirectionalView
    confidence: Decimal = Field(ge=0, le=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def identity_matches_frozen_source(self):
        payload = self.model_dump(
            mode="json", exclude={"entry_id", "source_hash"}
        )
        if self.source_hash != content_hash(payload):
            raise ValueError("决策带条目来源哈希不一致")
        expected_id = stable_id("forecast_tape_entry", self.source_hash)
        if self.entry_id != expected_id:
            raise ValueError("决策带条目 ID 不一致")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        proposal: AnalysisProposal,
        forecast: DirectionalForecast,
        cycle_id: str,
        pipeline_version: str,
        source_run_id: str,
        available_at: datetime,
    ) -> ForecastTapeEntry:
        payload = {
            "proposal_id": proposal.proposal_id,
            "cycle_id": cycle_id,
            "pipeline_version": pipeline_version,
            "source_run_id": source_run_id,
            "symbol": proposal.symbol,
            "available_at": require_utc(available_at),
            "horizon_minutes": forecast.horizon_minutes,
            "directional_view": forecast.directional_view,
            "confidence": forecast.confidence,
        }
        source_hash = content_hash(payload)
        return cls(
            entry_id=stable_id("forecast_tape_entry", source_hash),
            source_hash=source_hash,
            **payload,
        )


class ForecastTapeExclusion(FrozenModel):
    cycle_id: str
    reason_code: Literal["CODEX_COMPLETION_MISSING_OR_AMBIGUOUS"]


class ForecastDecisionTape(FrozenModel):
    tape_id: str
    version: str = FORECAST_DECISION_TAPE_VERSION
    pipeline_version: str
    symbol: str
    window_start: datetime
    window_end: datetime
    entries: tuple[ForecastTapeEntry, ...]
    exclusions: tuple[ForecastTapeExclusion, ...] = ()
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_window_start = field_validator("window_start")(require_utc)
    _utc_window_end = field_validator("window_end")(require_utc)

    @model_validator(mode="after")
    def bounds_order_and_hash_match(self):
        if self.window_start >= self.window_end:
            raise ValueError("决策带窗口起点必须早于终点")
        order = tuple(
            (item.available_at, item.horizon_minutes, item.entry_id)
            for item in self.entries
        )
        if order != tuple(sorted(order)) or len({item.entry_id for item in self.entries}) != len(
            self.entries
        ):
            raise ValueError("决策带条目必须唯一且按可用时间排序")
        if any(
            item.pipeline_version != self.pipeline_version
            or item.symbol != self.symbol
            or not self.window_start <= item.available_at < self.window_end
            for item in self.entries
        ):
            raise ValueError("决策带包含窗口或作用域外条目")
        payload = self.model_dump(mode="json", exclude={"tape_id", "content_hash"})
        if self.content_hash != content_hash(payload):
            raise ValueError("决策带内容哈希不一致")
        if self.tape_id != stable_id("forecast_decision_tape", self.content_hash):
            raise ValueError("决策带 ID 不一致")
        return self


class SqlForecastDecisionTapeReader:
    """只投影当时已存在的 Proposal 与成功完成事实；绝不读取结果标签。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(
        self,
        *,
        pipeline_version: str,
        symbol: str,
        window_start: datetime,
        window_end: datetime,
        maximum_completion_lag_seconds: int,
    ) -> ForecastDecisionTape:
        start = require_utc(window_start)
        end = require_utc(window_end)
        if start >= end:
            raise ValueError("决策带窗口起点必须早于终点")
        if maximum_completion_lag_seconds <= 0:
            raise ValueError("Codex 最大完成延迟必须为正数")
        query_start = start - timedelta(seconds=maximum_completion_lag_seconds)
        with self._engine.connect() as connection:
            rows = tuple(
                connection.execute(
                    select(
                        analysis_proposals.c.payload.label("proposal"),
                        analysis_cycles.c.cycle_id,
                        analysis_cycles.c.pipeline_version,
                        analysis_cycles.c.as_of,
                    )
                    .join(
                        analysis_cycles,
                        analysis_cycles.c.cycle_id == analysis_proposals.c.cycle_id,
                    )
                    .join(
                        market_snapshots,
                        market_snapshots.c.cycle_id == analysis_cycles.c.cycle_id,
                    )
                    .where(
                        analysis_cycles.c.pipeline_version == pipeline_version,
                        market_snapshots.c.symbol == symbol,
                        analysis_cycles.c.as_of >= query_start,
                        analysis_cycles.c.as_of < end,
                    )
                    .order_by(analysis_cycles.c.as_of, analysis_cycles.c.cycle_id)
                ).mappings()
            )
            cycle_ids = tuple(str(item["cycle_id"]) for item in rows)
            attempts = (
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
        attempts_by_cycle: dict[str, list[dict]] = {}
        for attempt in attempts:
            attempts_by_cycle.setdefault(str(attempt["cycle_id"]), []).append(
                dict(attempt)
            )

        entries: list[ForecastTapeEntry] = []
        exclusions: list[ForecastTapeExclusion] = []
        for row in rows:
            cycle_id = str(row["cycle_id"])
            analysis_as_of = database_utc(row["as_of"])
            available_at, source_run_id, _ = unique_successful_codex_completion(
                attempts_by_cycle.get(cycle_id, []),
                analysis_as_of=analysis_as_of,
            )
            if available_at is None or source_run_id is None:
                exclusions.append(
                    ForecastTapeExclusion(
                        cycle_id=cycle_id,
                        reason_code="CODEX_COMPLETION_MISSING_OR_AMBIGUOUS",
                    )
                )
                continue
            if not start <= available_at < end:
                continue
            proposal = AnalysisProposal.model_validate(row["proposal"])
            entries.extend(
                ForecastTapeEntry.freeze(
                    proposal=proposal,
                    forecast=forecast,
                    cycle_id=cycle_id,
                    pipeline_version=str(row["pipeline_version"]),
                    source_run_id=source_run_id,
                    available_at=available_at,
                )
                for forecast in proposal.forecasts
            )
        ordered = tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.available_at,
                    item.horizon_minutes,
                    item.entry_id,
                ),
            )
        )
        payload = {
            "version": FORECAST_DECISION_TAPE_VERSION,
            "pipeline_version": pipeline_version,
            "symbol": symbol,
            "window_start": start,
            "window_end": end,
            "entries": ordered,
            "exclusions": tuple(exclusions),
        }
        digest = content_hash(payload)
        return ForecastDecisionTape(
            tape_id=stable_id("forecast_decision_tape", digest),
            content_hash=digest,
            **payload,
        )


class ForecastGatePolicy(FrozenModel):
    plan_id: str
    version: str = "directional-alignment-context-gate-v2"
    forecast_role: Literal["INDEPENDENT_CONTEXT"] = "INDEPENDENT_CONTEXT"
    program_evaluation_clock: Literal["BAR_CLOSE"] = "BAR_CLOSE"
    registered_at: datetime
    evaluation_end: datetime
    horizon_minutes: int = Field(gt=0)
    maximum_age_minutes: int = Field(gt=0)
    minimum_confidence: Decimal = Field(ge=0, le=1)
    minimum_non_overlapping_forecasts: int = Field(default=30, ge=2)

    _utc_registered_at = field_validator("registered_at")(require_utc)
    _utc_evaluation_end = field_validator("evaluation_end")(require_utc)

    @model_validator(mode="after")
    def age_does_not_outlive_forecast(self):
        if self.evaluation_end <= self.registered_at:
            raise ValueError("AI 门控评价终点必须晚于预登记时间")
        if self.maximum_age_minutes > self.horizon_minutes:
            raise ValueError("AI 门控使用年龄不能超过预测周期")
        return self


class ForecastGateEvaluationSpec(FrozenModel):
    """Pre-registration identity for both Q and its deterministic AI gate."""

    version: str = "forecast-gate-evaluation-spec-v6"
    decision_tape_version: Literal["forecast-decision-tape-v1"] = (
        FORECAST_DECISION_TAPE_VERSION
    )
    paired_evaluation_version: Literal["paired-decision-tape-evaluation-v4"] = (
        PAIRED_DECISION_TAPE_EVALUATION_VERSION
    )
    base_strategy_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ai_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_blind_evaluation_id: str | None = None
    source_blind_evaluation_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    symbol: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    interval: str = Field(pattern=r"^(1m|3m|5m|15m|30m|1h|2h|4h|1d)$")
    data_source: Literal["binance-rest-historical"] = "binance-rest-historical"
    starting_equity: Decimal = Field(gt=0)
    spread_bps: Decimal = Field(ge=0)
    maximum_completion_lag_seconds: int = Field(gt=0)
    policy: ForecastGatePolicy

    @model_validator(mode="after")
    def v6_requires_passed_blind_source_identity(self):
        if self.version == "forecast-gate-evaluation-spec-v6" and (
            self.source_blind_evaluation_id is None
            or self.source_blind_evaluation_hash is None
        ):
            raise ValueError("v6 AI 门控计划必须绑定已通过的盲测基线")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        strategy: ResearchStrategy,
        config: AppConfig,
        symbol: str,
        pipeline_version: str,
        starting_equity: Decimal,
        spread_bps: Decimal,
        maximum_completion_lag_seconds: int,
        policy: ForecastGatePolicy,
        source_blind_evaluation_id: str,
        source_blind_evaluation_hash: str,
    ) -> ForecastGateEvaluationSpec:
        if pipeline_version != config.pipeline.version:
            raise ValueError("AI 门控 Pipeline 必须与冻结配置完全一致")
        return cls(
            base_strategy_spec_hash=content_hash(strategy.research_spec),
            research_artifact_hash=artifact_hash(
                config,
                strategy_spec=strategy.research_spec,
            ),
            ai_artifact_hash=content_hash(
                {
                    "pipeline": config.pipeline,
                    "panel": config.panel,
                    "proposal": config.proposal,
                    "codex_runtime": config.codex_runtime,
                    "information": config.information,
                    "trigger": config.trigger,
                    "market_data": config.market_data,
                    "analyst_input_version": ANALYST_INPUT_VERSION,
                }
            ),
            source_blind_evaluation_id=source_blind_evaluation_id,
            source_blind_evaluation_hash=source_blind_evaluation_hash,
            symbol=symbol,
            pipeline_version=pipeline_version,
            interval=config.market_data.interval,
            starting_equity=starting_equity,
            spread_bps=spread_bps,
            maximum_completion_lag_seconds=maximum_completion_lag_seconds,
            policy=policy,
        )


def validate_forecast_gate_baseline(
    *,
    source: BlindEvaluationResult,
    config: AppConfig,
    strategy: ResearchStrategy,
    symbol: str,
) -> None:
    if not source.completed or not source.passed:
        raise ValueError("AI 门控只能使用已通过的一次性盲测基线")
    strategy_spec = strategy.research_spec
    if not isinstance(strategy_spec, BaseModel):
        raise ValueError("AI 门控基线策略规格必须可序列化")
    expected_spec = strategy_spec.model_dump(mode="json")
    if source.strategy_spec_snapshot != expected_spec:
        raise ValueError("AI 门控基线与盲测策略规格不一致")
    if source.artifact_hash != artifact_hash(
        config,
        strategy_spec=strategy_spec,
    ):
        raise ValueError("AI 门控基线的代码、风控或成本语义已变更")
    if source.run.symbol != symbol or source.run.interval != config.market_data.interval:
        raise ValueError("AI 门控基线的品种或周期与配对评价不一致")


def build_forecast_gate_evaluation_plan(
    *,
    spec: ForecastGateEvaluationSpec,
    base_manifest_id: str,
) -> EvaluationPlan:
    return EvaluationPlan(
        plan_id=spec.policy.plan_id,
        registered_at=spec.policy.registered_at,
        base_manifest_id=base_manifest_id,
        primary_metric="incremental_net_pnl",
        minimum_sample_size=spec.policy.minimum_non_overlapping_forecasts,
        hard_guardrails=(
            "INCREMENTAL_NET_PNL_POSITIVE",
            "MAXIMUM_DRAWDOWN_NOT_WORSE",
            "NON_OVERLAPPING_FORECAST_SAMPLE_MINIMUM",
        ),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
            EvaluationStage.SHADOW,
        ),
        fixed_regression_suite_version="quant-core-paired-context-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=0,
    )


def validate_forecast_gate_evaluation_plan(
    *,
    spec: ForecastGateEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
) -> None:
    if plan.plan_id != spec.policy.plan_id or plan.registered_at != spec.policy.registered_at:
        raise ValueError("AI 门控规格与预登记计划身份不一致")
    if plan.base_manifest_id != champion_manifest_id:
        raise ValueError("AI 门控计划不属于当前 Champion")
    if plan.candidate_spec_hash != content_hash(spec):
        raise ValueError("AI 门控参数、程序基线或数据边界与预登记规格不一致")
    if plan.minimum_sample_size != spec.policy.minimum_non_overlapping_forecasts:
        raise ValueError("AI 门控非重叠样本下限与预登记计划不一致")
    required = {
        EvaluationStage.STATIC,
        EvaluationStage.FIXED_REGRESSION,
        EvaluationStage.WALK_FORWARD,
        EvaluationStage.SHADOW,
    }
    if not required.issubset(plan.required_stages):
        raise ValueError("AI 门控预登记计划缺少必要评价阶段")


class ForecastGatedStrategySpec(FrozenModel):
    version: str = "forecast-gated-research-strategy-v2"
    base_strategy_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ForecastGatePolicy
    tape_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForecastGatedStrategy:
    """在基础 Q 生成候选后，只用当时已经可见的预测做方向门控。"""

    def __init__(
        self,
        base: ResearchStrategy,
        *,
        policy: ForecastGatePolicy,
        tape: ForecastDecisionTape,
    ) -> None:
        self._base = base
        self._policy = policy
        self._entries = tuple(
            item
            for item in tape.entries
            if item.horizon_minutes == policy.horizon_minutes
            and item.available_at >= policy.registered_at
        )
        self._available_times = tuple(item.available_at for item in self._entries)
        self._spec = ForecastGatedStrategySpec(
            base_strategy_spec_hash=content_hash(base.research_spec),
            policy=policy,
            tape_hash=tape.content_hash,
        )

    @property
    def research_spec(self) -> ForecastGatedStrategySpec:
        return self._spec

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]:
        candidates = self._base.evaluate(
            market=market,
            account=account,
            features=features,
            events=events,
        )
        if not candidates:
            return ()
        index = bisect_right(self._available_times, market.as_of) - 1
        if index < 0:
            return ()
        forecast = self._entries[index]
        if (
            forecast.symbol != market.symbol
            or market.as_of - forecast.available_at
            > timedelta(minutes=self._policy.maximum_age_minutes)
            or forecast.confidence < self._policy.minimum_confidence
        ):
            return ()
        return tuple(
            candidate
            for candidate in candidates
            if forecast.directional_view
            == (DirectionalView.UP if candidate.side == Side.BUY else DirectionalView.DOWN)
        )


class PairedDecisionTapeResult(FrozenModel):
    evaluation_id: str
    version: str = PAIRED_DECISION_TAPE_EVALUATION_VERSION
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ForecastGatePolicy
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tape_id: str
    tape_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tape_entry_ids: tuple[str, ...] = Field(min_length=1)
    non_overlapping_forecast_count: int = Field(ge=0)
    minimum_non_overlapping_forecasts: int = Field(ge=2)
    evidence_sufficient: bool
    baseline: BacktestRun
    gated: BacktestRun
    incremental_net_pnl: Decimal
    incremental_return_fraction: Decimal
    maximum_drawdown_change: Decimal
    trade_count_change: int
    common_candidate_count: int = Field(ge=0)
    baseline_only_candidate_count: int = Field(ge=0)
    gated_only_candidate_count: int = Field(ge=0)
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def identity_and_pair_match(self):
        if self.policy_hash != content_hash(self.policy):
            raise ValueError("配对回放门控策略哈希不一致")
        if len(self.tape_entry_ids) != len(set(self.tape_entry_ids)):
            raise ValueError("配对回放决策带条目不得重复")
        if self.evidence_sufficient != (
            self.non_overlapping_forecast_count
            >= self.minimum_non_overlapping_forecasts
        ):
            raise ValueError("配对回放证据充分性与非重叠样本数不一致")
        if (
            self.minimum_non_overlapping_forecasts
            != self.policy.minimum_non_overlapping_forecasts
        ):
            raise ValueError("配对回放最小样本数与预登记策略不一致")
        if (
            self.baseline.dataset_id != self.gated.dataset_id
            or self.baseline.event_dataset_id != self.gated.event_dataset_id
            or self.baseline.symbol != self.gated.symbol
            or self.baseline.signal_start != self.gated.signal_start
            or self.baseline.signal_end != self.gated.signal_end
        ):
            raise ValueError("配对回放必须使用相同数据集、品种和窗口")
        payload = self.model_dump(mode="json", exclude={"evaluation_id"})
        if self.evaluation_id != stable_id("paired_decision_tape", content_hash(payload)):
            raise ValueError("配对决策带评价 ID 不一致")
        return self


def run_paired_decision_tape_backtest(
    *,
    dataset: HistoricalDataset,
    config: AppConfig,
    tape: ForecastDecisionTape,
    policy: ForecastGatePolicy,
    strategy: ResearchStrategy,
    signal_start: datetime,
    signal_end: datetime,
    replay_start: datetime | None = None,
    replay_end: datetime | None = None,
    starting_equity: Decimal = Decimal("10000"),
    spread_bps: Decimal = Decimal("1"),
    evaluation_spec_hash: str,
) -> PairedDecisionTapeResult:
    start = require_utc(signal_start)
    end = require_utc(signal_end)
    if start < policy.registered_at:
        raise ValueError("配对评价窗口不能早于门控策略预登记时间")
    if start != policy.registered_at or end != policy.evaluation_end:
        raise ValueError("配对评价窗口必须与预登记起止时间完全一致")
    if tape.symbol != dataset.manifest.symbol:
        raise ValueError("决策带与历史数据品种不一致")
    if (
        dataset.manifest.first_open_time > start
        or dataset.manifest.last_close_time < end
    ):
        raise ValueError("历史行情数据集未覆盖完整配对评价窗口")
    if tape.window_start > start or tape.window_end < end:
        raise ValueError("Codex 决策带未覆盖完整配对评价窗口")
    eligible = tuple(
        item
        for item in tape.entries
        if item.horizon_minutes == policy.horizon_minutes
        and policy.registered_at <= item.available_at < end
    )
    if not eligible:
        raise ValueError("评价窗口没有预登记后首次生成的同周期预测")
    non_overlapping = _non_overlapping_forecast_count(
        eligible,
        horizon_minutes=policy.horizon_minutes,
    )

    common = {
        "dataset": dataset,
        "config": config,
        "signal_start": start,
        "signal_end": end,
        "replay_start": replay_start,
        "replay_end": replay_end,
        "starting_equity": starting_equity,
        "spread_bps": spread_bps,
    }
    baseline = run_bar_backtest(strategy=strategy, **common)
    gated = run_bar_backtest(
        strategy=ForecastGatedStrategy(strategy, policy=policy, tape=tape),
        **common,
    )
    baseline_ids = {item.candidate_id for item in baseline.trades}
    gated_ids = {item.candidate_id for item in gated.trades}
    policy_hash = content_hash(policy)
    payload = {
        "version": PAIRED_DECISION_TAPE_EVALUATION_VERSION,
        "evaluation_spec_hash": evaluation_spec_hash,
        "policy": policy,
        "policy_hash": policy_hash,
        "tape_id": tape.tape_id,
        "tape_hash": tape.content_hash,
        "tape_entry_ids": tuple(item.entry_id for item in eligible),
        "non_overlapping_forecast_count": non_overlapping,
        "minimum_non_overlapping_forecasts": (
            policy.minimum_non_overlapping_forecasts
        ),
        "evidence_sufficient": (
            non_overlapping >= policy.minimum_non_overlapping_forecasts
        ),
        "baseline": baseline,
        "gated": gated,
        "incremental_net_pnl": gated.metrics.net_pnl - baseline.metrics.net_pnl,
        "incremental_return_fraction": (
            gated.metrics.return_fraction - baseline.metrics.return_fraction
        ),
        "maximum_drawdown_change": (
            gated.metrics.maximum_drawdown_fraction
            - baseline.metrics.maximum_drawdown_fraction
        ),
        "trade_count_change": gated.metrics.trade_count - baseline.metrics.trade_count,
        "common_candidate_count": len(baseline_ids & gated_ids),
        "baseline_only_candidate_count": len(baseline_ids - gated_ids),
        "gated_only_candidate_count": len(gated_ids - baseline_ids),
        "limitations": (
            "FORWARD_FROZEN_CODEX_OUTPUTS_ONLY",
            "INDEPENDENT_CONTEXT_FORECAST_ONLY",
            "PROGRAM_SIGNAL_CLOCK_IS_BAR_CLOSE",
            "PRODUCTION_TRIGGER_CLOCK_NOT_REPLAYED",
            "HOSTED_MODEL_SNAPSHOT_NOT_AUDITABLE",
            "PAIRED_PATHS_MAY_DIVERGE_AFTER_GATE_DECISIONS",
            "NO_AI_OUTPUT_REGENERATION",
        ),
    }
    return PairedDecisionTapeResult(
        evaluation_id=stable_id("paired_decision_tape", content_hash(payload)),
        **payload,
    )


def _non_overlapping_forecast_count(
    entries: tuple[ForecastTapeEntry, ...],
    *,
    horizon_minutes: int,
) -> int:
    count = 0
    next_available_at: datetime | None = None
    horizon = timedelta(minutes=horizon_minutes)
    for entry in entries:
        if next_available_at is not None and entry.available_at < next_available_at:
            continue
        count += 1
        next_available_at = entry.available_at + horizon
    return count
