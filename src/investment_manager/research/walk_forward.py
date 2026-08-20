from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from investment_manager.governance.models import EvaluationPlan, EvaluationStage, FailedExperiment
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.legacy.strategy import PriceTrendStrategy
from investment_manager.research.backtest import (
    BacktestMetrics,
    BacktestRun,
    BacktestTrade,
    ResearchStrategy,
    _mean_lower_bound,
    artifact_hash,
    run_bar_backtest,
)
from investment_manager.research.dataset import (
    HistoricalDataset,
    HistoricalEventDataset,
    HistoricalFundingDataset,
)
from investment_manager.settings import AppConfig

WALK_FORWARD_EVALUATION_SPEC_VERSION = "walk-forward-evaluation-spec-v2"
BLIND_EVALUATION_VERSION = "blind-evaluation-v1"
RESEARCH_REGRESSION_SUITE_VERSION = "quant-core-research-regression-v1"


class WalkForwardPlan(FrozenModel):
    plan_id: str
    training_bars: int = Field(ge=2)
    test_bars: int = Field(ge=2)
    blind_bars: int = Field(default=0, ge=0)
    starting_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    spread_bps: Decimal = Field(default=Decimal("1"), ge=0)
    minimum_trades: int = Field(default=30, ge=1)
    minimum_profit_factor: Decimal = Field(default=Decimal("1.05"), gt=0)
    minimum_average_net_return_bps_lower_bound: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    minimum_positive_fold_fraction: Decimal = Field(
        default=Decimal("0.75"), gt=0, le=1
    )


class WalkForwardEvaluationSpec(FrozenModel):
    """完整冻结一次可执行的程序策略研究，供事实库预登记校验。"""

    version: str = WALK_FORWARD_EVALUATION_SPEC_VERSION
    candidate: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    event_dataset_id: str | None = None
    funding_dataset_id: str | None = None
    research_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: WalkForwardPlan

    @classmethod
    def freeze(
        cls,
        *,
        candidate: str,
        dataset: HistoricalDataset,
        event_dataset: HistoricalEventDataset | None,
        funding_dataset: HistoricalFundingDataset | None = None,
        config: AppConfig,
        strategy: ResearchStrategy,
        plan: WalkForwardPlan,
    ) -> WalkForwardEvaluationSpec:
        return cls(
            candidate=candidate,
            dataset_id=dataset.manifest.dataset_id,
            event_dataset_id=(
                event_dataset.manifest.dataset_id if event_dataset is not None else None
            ),
            funding_dataset_id=(
                funding_dataset.manifest.dataset_id
                if funding_dataset is not None
                else None
            ),
            research_artifact_hash=artifact_hash(
                config,
                strategy_spec=strategy.research_spec,
            ),
            plan=plan,
        )


def build_walk_forward_evaluation_plan(
    *,
    spec: WalkForwardEvaluationSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    stages = [
        EvaluationStage.STATIC,
        EvaluationStage.FIXED_REGRESSION,
        EvaluationStage.WALK_FORWARD,
    ]
    if spec.plan.blind_bars:
        stages.append(EvaluationStage.BLIND)
    return EvaluationPlan(
        plan_id=spec.plan.plan_id,
        registered_at=require_utc(registered_at),
        base_manifest_id=base_manifest_id,
        primary_metric="average_net_return_bps_lower_bound",
        minimum_sample_size=spec.plan.minimum_trades,
        hard_guardrails=(
            "NET_PNL_POSITIVE",
            "NET_RETURN_LOWER_CONFIDENCE_BOUND_POSITIVE",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
            "POSITIVE_FOLD_FRACTION_WITHIN_LIMIT",
        ),
        required_stages=tuple(stages),
        fixed_regression_suite_version=RESEARCH_REGRESSION_SUITE_VERSION,
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=1 if spec.plan.blind_bars else 0,
    )


def validate_walk_forward_evaluation_plan(
    *,
    spec: WalkForwardEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
    evaluated_at: datetime,
) -> None:
    if plan.plan_id != spec.plan.plan_id:
        raise ValueError("walk-forward 与预登记计划 ID 不一致")
    if plan.registered_at > require_utc(evaluated_at):
        raise ValueError("walk-forward 不能早于计划预登记时间")
    if plan.base_manifest_id != champion_manifest_id:
        raise ValueError("walk-forward 计划不属于当前 Champion")
    if plan.candidate_spec_hash != content_hash(spec):
        raise ValueError("walk-forward 参数、候选或数据与预登记规格不一致")
    if plan.minimum_sample_size != spec.plan.minimum_trades:
        raise ValueError("walk-forward 最小交易样本数与预登记计划不一致")
    required = {
        EvaluationStage.STATIC,
        EvaluationStage.FIXED_REGRESSION,
        EvaluationStage.WALK_FORWARD,
    }
    if not required.issubset(plan.required_stages):
        raise ValueError("walk-forward 预登记计划缺少必要评价阶段")
    if spec.plan.blind_bars and (
        EvaluationStage.BLIND not in plan.required_stages
        or plan.blind_query_budget != 1
    ):
        raise ValueError("walk-forward 盲区没有预登记查询预算")


class WalkForwardFold(FrozenModel):
    fold_id: str
    training_start: datetime
    training_end: datetime
    test_start: datetime
    test_end: datetime
    embargo_bars: int = Field(gt=0)
    purge_bars: int = Field(gt=0)
    run: BacktestRun

    _utc_training_start = field_validator("training_start")(require_utc)
    _utc_training_end = field_validator("training_end")(require_utc)
    _utc_test_start = field_validator("test_start")(require_utc)
    _utc_test_end = field_validator("test_end")(require_utc)

    @model_validator(mode="after")
    def windows_are_separated(self):
        if not self.training_start < self.training_end < self.test_start < self.test_end:
            raise ValueError("walk-forward 时间窗口顺序非法")
        return self


class WalkForwardResult(FrozenModel):
    evaluation_id: str
    plan: WalkForwardPlan
    evaluation_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dataset_id: str
    event_dataset_id: str | None = None
    funding_dataset_id: str | None = None
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Old catalog entries predate this field. New evaluations freeze the exact
    # candidate parameters so a failed named candidate can be removed from code.
    strategy_spec_snapshot: dict[str, object] | None = None
    embargo_bars: int = Field(gt=0)
    purge_bars: int = Field(gt=0)
    blind_start: datetime | None = None
    blind_end: datetime | None = None
    blind_bar_count: int = Field(ge=0)
    completed: bool
    passed: bool
    reason_codes: tuple[str, ...]
    folds: tuple[WalkForwardFold, ...] = Field(min_length=1)
    metrics: BacktestMetrics

    @model_validator(mode="after")
    def fold_data_provenance_matches(self):
        if any(
            item.run.dataset_id != self.dataset_id
            or item.run.event_dataset_id != self.event_dataset_id
            or item.run.funding_dataset_id != self.funding_dataset_id
            for item in self.folds
        ):
            raise ValueError("walk-forward 折叠数据制品身份不一致")
        return self


class BlindEvaluationResult(FrozenModel):
    """The sole replay of a preregistered result's reserved blind window."""

    version: str = BLIND_EVALUATION_VERSION
    result_id: str
    query_id: str
    source_evaluation_id: str
    plan: WalkForwardPlan
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str
    event_dataset_id: str | None = None
    funding_dataset_id: str | None = None
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_spec_snapshot: dict[str, object]
    reserved_start: datetime
    reserved_end: datetime
    embargo_bars: int = Field(gt=0)
    purge_bars: int = Field(gt=0)
    completed: bool
    passed: bool
    reason_codes: tuple[str, ...]
    run: BacktestRun

    _utc_reserved_start = field_validator("reserved_start")(require_utc)
    _utc_reserved_end = field_validator("reserved_end")(require_utc)

    @model_validator(mode="after")
    def identity_and_provenance_match(self):
        if self.reserved_start >= self.reserved_end:
            raise ValueError("盲测预留窗口时间范围非法")
        if (
            self.run.dataset_id != self.dataset_id
            or self.run.event_dataset_id != self.event_dataset_id
            or self.run.funding_dataset_id != self.funding_dataset_id
        ):
            raise ValueError("盲测运行的数据制品身份不一致")
        expected = stable_id(
            "blind_evaluation",
            self.version,
            self.query_id,
            self.source_evaluation_id,
            self.evaluation_spec_hash,
            self.dataset_id,
            self.event_dataset_id,
            self.funding_dataset_id,
            self.artifact_hash,
            self.run.run_id,
        )
        if self.result_id != expected:
            raise ValueError("盲测结果 ID 与冻结内容不一致")
        return self


def failed_walk_forward_experiment(
    result: WalkForwardResult,
    *,
    rejected_at: datetime,
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的 walk-forward 不能登记为失败实验")
    hypothesis = _historical_hypothesis(
        result.strategy_spec_snapshot,
        symbol=result.folds[0].run.symbol,
        interval=result.folds[0].run.interval,
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_walk_forward", result.evaluation_id),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(f"hypothesis:{hypothesis}", result.evaluation_id),
        rejected_at=require_utc(rejected_at),
        reason_codes=("WALK_FORWARD_FAILED", *result.reason_codes),
    )


def failed_blind_experiment(
    result: BlindEvaluationResult,
    *,
    rejected_at: datetime,
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的盲测不能登记为失败实验")
    hypothesis = _historical_hypothesis(
        result.strategy_spec_snapshot,
        symbol=result.run.symbol,
        interval=result.run.interval,
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_blind", result.result_id),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(
            f"hypothesis:{hypothesis}",
            result.source_evaluation_id,
            result.result_id,
        ),
        rejected_at=require_utc(rejected_at),
        reason_codes=("BLIND_FAILED", *result.reason_codes),
    )


def _historical_hypothesis(
    strategy_spec: dict[str, object] | None,
    *,
    symbol: str,
    interval: str,
) -> str:
    snapshot = strategy_spec or {}
    family = str(snapshot.get("family") or snapshot.get("strategy_id") or "unknown")
    version = str(snapshot.get("version") or "unknown")
    return (
        f"历史程序策略 {family}/{version} 在 {symbol} {interval} "
        "满足预登记的费用后收益、稳定性和风险门槛"
    )


@dataclass(frozen=True)
class BlindEvaluationScope:
    symbol: str
    start: datetime
    end: datetime

    @property
    def scope_id(self) -> str:
        return stable_id("blind_evaluation_scope", self.symbol, self.start, self.end)


def blind_evaluation_scope(source: WalkForwardResult) -> BlindEvaluationScope:
    """Identify one market outcome window independently of candidate or dataset copy."""

    if source.blind_start is None or source.blind_end is None:
        raise ValueError("walk-forward 没有有效的预留盲区")
    symbols = {fold.run.symbol for fold in source.folds}
    if len(symbols) != 1:
        raise ValueError("walk-forward 盲测范围必须只包含一个品种")
    return BlindEvaluationScope(
        symbol=next(iter(symbols)),
        start=source.blind_start,
        end=source.blind_end,
    )


def run_walk_forward(
    *,
    dataset: HistoricalDataset,
    event_dataset: HistoricalEventDataset | None = None,
    funding_dataset: HistoricalFundingDataset | None = None,
    config: AppConfig,
    plan: WalkForwardPlan,
    strategy: ResearchStrategy | None = None,
    evaluation_spec_hash: str | None = None,
) -> WalkForwardResult:
    """对一个冻结候选做扩展训练窗、互斥样本外窗口评价；不自动调参。"""

    bars = dataset.bars
    interval_minutes = config.market_data.interval_seconds // 60
    if dataset.manifest.interval != config.market_data.interval:
        raise ValueError("历史数据周期必须与当前 MarketDataPolicy 一致")
    core_strategy: ResearchStrategy = strategy or PriceTrendStrategy(config.strategy)
    strategy_spec = core_strategy.research_spec
    if not isinstance(strategy_spec, BaseModel):
        raise ValueError("历史策略 research_spec 必须是可序列化的 Pydantic 模型")
    strategy_spec_snapshot = strategy_spec.model_dump(mode="json")
    horizon_minutes = getattr(strategy_spec, "horizon_minutes", None)
    if not isinstance(horizon_minutes, int) or horizon_minutes <= 0:
        raise ValueError("历史策略 research_spec 必须声明正整数 horizon_minutes")
    horizon_bars = _ceil_div(horizon_minutes, interval_minutes)
    # 特征预热只读取信号时点以前的数据，不是泄漏。隔离仅覆盖下一开盘成交与标签跨度。
    embargo_bars = horizon_bars + 1
    # 下一根开盘成交、覆盖完整持有期，再留一根给撮合事件结算；标签仍不跨 test_end。
    purge_bars = horizon_bars + 2
    if plan.test_bars <= purge_bars:
        raise ValueError("样本外窗口必须长于持有期 purge")

    evaluation_bar_count = len(bars) - plan.blind_bars
    if evaluation_bar_count <= 0:
        raise ValueError("盲测预留不能耗尽全部历史数据")
    blind_start = bars[evaluation_bar_count].open_time if plan.blind_bars else None
    blind_end = bars[-1].close_time if plan.blind_bars else None

    folds: list[WalkForwardFold] = []
    training_end = plan.training_bars
    while True:
        test_start_index = training_end + embargo_bars
        test_end_index = test_start_index + plan.test_bars
        if test_end_index > evaluation_bar_count:
            break
        signal_end_index = test_end_index - purge_bars
        replay_start_index = max(0, test_start_index - config.market_data.bar_window)
        replay_end = bars[test_end_index - 1].close_time + timedelta(microseconds=1)
        signal_start = bars[test_start_index].close_time
        signal_end = bars[signal_end_index].close_time
        run = run_bar_backtest(
            dataset=dataset,
            event_dataset=event_dataset,
            funding_dataset=funding_dataset,
            config=config,
            signal_start=signal_start,
            signal_end=signal_end,
            replay_start=bars[replay_start_index].open_time,
            replay_end=replay_end,
            starting_equity=plan.starting_equity,
            spread_bps=plan.spread_bps,
            strategy=core_strategy,
        )
        fold_id = stable_id(
            "walk_forward_fold",
            plan.plan_id,
            len(folds),
            bars[0].open_time,
            bars[training_end - 1].close_time,
            signal_start,
            replay_end,
        )
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                training_start=bars[0].open_time,
                training_end=bars[training_end - 1].close_time,
                test_start=signal_start,
                test_end=replay_end,
                embargo_bars=embargo_bars,
                purge_bars=purge_bars,
                run=run,
            )
        )
        training_end = test_end_index

    if not folds:
        required = plan.training_bars + embargo_bars + plan.test_bars
        raise ValueError(f"历史数据不足以形成一个 walk-forward 窗口；至少需要 {required} 根")

    metrics = _aggregate_metrics(tuple(folds), plan.starting_equity)
    frozen_artifact = artifact_hash(config, strategy_spec=strategy_spec)
    evaluation_id = stable_id(
        "walk_forward_evaluation",
        plan,
        evaluation_spec_hash,
        dataset.manifest.dataset_id,
        funding_dataset.manifest.dataset_id if funding_dataset is not None else None,
        frozen_artifact,
        [item.run.run_id for item in folds],
    )
    completed = all(item.run.completed for item in folds)
    reasons = _gate_reasons(
        folds=tuple(folds),
        metrics=metrics,
        plan=plan,
        completed=completed,
    )
    return WalkForwardResult(
        evaluation_id=evaluation_id,
        plan=plan,
        evaluation_spec_hash=evaluation_spec_hash,
        dataset_id=dataset.manifest.dataset_id,
        event_dataset_id=(
            event_dataset.manifest.dataset_id if event_dataset is not None else None
        ),
        funding_dataset_id=(
            funding_dataset.manifest.dataset_id
            if funding_dataset is not None
            else None
        ),
        artifact_hash=frozen_artifact,
        strategy_spec_snapshot=strategy_spec_snapshot,
        embargo_bars=embargo_bars,
        purge_bars=purge_bars,
        blind_start=blind_start,
        blind_end=blind_end,
        blind_bar_count=plan.blind_bars,
        completed=completed,
        passed=not reasons,
        reason_codes=tuple(reasons) or ("ALL_PREREGISTERED_GATES_PASSED",),
        folds=tuple(folds),
        metrics=metrics,
    )


def run_blind_evaluation(
    *,
    source: WalkForwardResult,
    query_id: str,
    dataset: HistoricalDataset,
    event_dataset: HistoricalEventDataset | None,
    funding_dataset: HistoricalFundingDataset | None = None,
    config: AppConfig,
    strategy: ResearchStrategy,
) -> BlindEvaluationResult:
    """Reveal and replay one reserved tail only after walk-forward has passed."""

    if not source.completed or not source.passed:
        raise ValueError("只有通过全部预登记门禁的 walk-forward 才能揭盲")
    if source.evaluation_spec_hash is None:
        raise ValueError("旧版未冻结完整规格的 walk-forward 不能揭盲")
    if source.plan.blind_bars <= 0 or source.blind_bar_count != source.plan.blind_bars:
        raise ValueError("walk-forward 没有有效的预留盲区")
    if dataset.manifest.dataset_id != source.dataset_id:
        raise ValueError("盲测数据集与 walk-forward 不一致")
    observed_event_dataset_id = (
        event_dataset.manifest.dataset_id if event_dataset is not None else None
    )
    if observed_event_dataset_id != source.event_dataset_id:
        raise ValueError("盲测事件数据集与 walk-forward 不一致")
    observed_funding_dataset_id = (
        funding_dataset.manifest.dataset_id if funding_dataset is not None else None
    )
    if observed_funding_dataset_id != source.funding_dataset_id:
        raise ValueError("盲测资金费率数据集与 walk-forward 不一致")
    if dataset.manifest.interval != config.market_data.interval:
        raise ValueError("历史数据周期必须与当前 MarketDataPolicy 一致")

    strategy_spec = strategy.research_spec
    if not isinstance(strategy_spec, BaseModel):
        raise ValueError("历史策略 research_spec 必须是可序列化的 Pydantic 模型")
    strategy_snapshot = strategy_spec.model_dump(mode="json")
    if strategy_snapshot != source.strategy_spec_snapshot:
        raise ValueError("盲测策略规格与 walk-forward 不一致")
    frozen_artifact = artifact_hash(config, strategy_spec=strategy_spec)
    if frozen_artifact != source.artifact_hash:
        raise ValueError("盲测代码、配置或成本模型与 walk-forward 不一致")

    bars = dataset.bars
    blind_start_index = len(bars) - source.blind_bar_count
    if blind_start_index <= 0:
        raise ValueError("预留盲区不能耗尽全部历史数据")
    reserved_start = bars[blind_start_index].open_time
    reserved_end = bars[-1].close_time
    if reserved_start != source.blind_start or reserved_end != source.blind_end:
        raise ValueError("盲测窗口与 walk-forward 冻结边界不一致")
    if source.folds[-1].test_end >= reserved_start:
        raise ValueError("walk-forward 样本外窗口侵入了预留盲区")

    interval_minutes = config.market_data.interval_seconds // 60
    horizon_minutes = getattr(strategy_spec, "horizon_minutes", None)
    if not isinstance(horizon_minutes, int) or horizon_minutes <= 0:
        raise ValueError("历史策略 research_spec 必须声明正整数 horizon_minutes")
    horizon_bars = _ceil_div(horizon_minutes, interval_minutes)
    embargo_bars = horizon_bars + 1
    purge_bars = horizon_bars + 2
    if embargo_bars != source.embargo_bars or purge_bars != source.purge_bars:
        raise ValueError("盲测隔离参数与 walk-forward 不一致")

    signal_start_index = blind_start_index + embargo_bars
    signal_end_index = len(bars) - purge_bars
    if signal_start_index >= signal_end_index:
        raise ValueError("预留盲区扣除 embargo/purge 后没有可评价窗口")
    replay_start_index = max(0, signal_start_index - config.market_data.bar_window)
    signal_start = bars[signal_start_index].close_time
    signal_end = bars[signal_end_index].close_time
    replay_end = bars[-1].close_time + timedelta(microseconds=1)
    run = run_bar_backtest(
        dataset=dataset,
        event_dataset=event_dataset,
        funding_dataset=funding_dataset,
        config=config,
        signal_start=signal_start,
        signal_end=signal_end,
        replay_start=bars[replay_start_index].open_time,
        replay_end=replay_end,
        starting_equity=source.plan.starting_equity,
        spread_bps=source.plan.spread_bps,
        strategy=strategy,
    )
    gate_fold = WalkForwardFold(
        fold_id=stable_id("blind_gate_fold", query_id, run.run_id),
        training_start=bars[0].open_time,
        training_end=bars[blind_start_index - 1].close_time,
        test_start=signal_start,
        test_end=replay_end,
        embargo_bars=embargo_bars,
        purge_bars=purge_bars,
        run=run,
    )
    reasons = _gate_reasons(
        folds=(gate_fold,),
        metrics=run.metrics,
        plan=source.plan,
        completed=run.completed,
    )
    result_id = stable_id(
        "blind_evaluation",
        BLIND_EVALUATION_VERSION,
        query_id,
        source.evaluation_id,
        source.evaluation_spec_hash,
        source.dataset_id,
        source.event_dataset_id,
        source.funding_dataset_id,
        frozen_artifact,
        run.run_id,
    )
    return BlindEvaluationResult(
        result_id=result_id,
        query_id=query_id,
        source_evaluation_id=source.evaluation_id,
        plan=source.plan,
        evaluation_spec_hash=source.evaluation_spec_hash,
        dataset_id=source.dataset_id,
        event_dataset_id=source.event_dataset_id,
        funding_dataset_id=source.funding_dataset_id,
        artifact_hash=frozen_artifact,
        strategy_spec_snapshot=strategy_snapshot,
        reserved_start=reserved_start,
        reserved_end=reserved_end,
        embargo_bars=embargo_bars,
        purge_bars=purge_bars,
        completed=run.completed,
        passed=not reasons,
        reason_codes=tuple(reasons) or ("ALL_PREREGISTERED_GATES_PASSED",),
        run=run,
    )


def _aggregate_metrics(
    folds: tuple[WalkForwardFold, ...], starting_equity: Decimal
) -> BacktestMetrics:
    trades: tuple[BacktestTrade, ...] = tuple(
        trade for fold in folds for trade in fold.run.trades
    )
    wins = [item.net_pnl for item in trades if item.net_pnl > 0]
    losses = [-item.net_pnl for item in trades if item.net_pnl < 0]
    total_start = starting_equity * len(folds)
    gross = sum((item.gross_pnl for item in trades), Decimal("0"))
    modeled_cost = sum((item.modeled_cost for item in trades), Decimal("0"))
    average_gross_bps = (
        sum((item.gross_return_bps for item in trades), Decimal("0")) / len(trades)
        if trades
        else None
    )
    average_net_bps = (
        sum((item.net_return_bps for item in trades), Decimal("0")) / len(trades)
        if trades
        else None
    )
    net = sum((item.run.metrics.net_pnl for item in folds), Decimal("0"))
    profit_factor = (
        sum(wins, Decimal("0")) / sum(losses, Decimal("0")) if losses else None
    )
    return BacktestMetrics(
        starting_equity=total_start,
        ending_equity=total_start + net,
        gross_pnl=gross,
        modeled_cost=modeled_cost,
        net_pnl=net,
        return_fraction=net / total_start,
        trade_count=len(trades),
        win_rate=(Decimal(len(wins)) / len(trades) if trades else None),
        profit_factor=profit_factor,
        maximum_drawdown_fraction=max(
            item.run.metrics.maximum_drawdown_fraction for item in folds
        ),
        average_gross_return_bps=average_gross_bps,
        average_modeled_cost_bps=(
            average_gross_bps - average_net_bps
            if average_gross_bps is not None and average_net_bps is not None
            else None
        ),
        average_net_return_bps=average_net_bps,
        average_net_return_bps_lower_bound=_mean_lower_bound(
            tuple(item.net_return_bps for item in trades)
        ),
        benchmark_buy_hold_bps=(
            sum((item.run.metrics.benchmark_buy_hold_bps for item in folds), Decimal("0"))
            / len(folds)
        ),
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _gate_reasons(
    *,
    folds: tuple[WalkForwardFold, ...],
    metrics: BacktestMetrics,
    plan: WalkForwardPlan,
    completed: bool,
) -> list[str]:
    reasons: list[str] = []
    if not completed:
        reasons.append("INCOMPLETE_BACKTEST")
    if metrics.trade_count < plan.minimum_trades:
        reasons.append("MINIMUM_TRADES_NOT_MET")
    if metrics.net_pnl <= 0:
        reasons.append("NET_PNL_NOT_POSITIVE")
    if (
        metrics.average_net_return_bps_lower_bound is None
        or metrics.average_net_return_bps_lower_bound
        <= plan.minimum_average_net_return_bps_lower_bound
    ):
        reasons.append("NET_RETURN_LOWER_CONFIDENCE_BOUND_NOT_POSITIVE")
    if (
        metrics.profit_factor is not None
        and metrics.profit_factor < plan.minimum_profit_factor
    ) or (metrics.profit_factor is None and metrics.net_pnl <= 0):
        reasons.append("PROFIT_FACTOR_BELOW_GATE")
    if metrics.maximum_drawdown_fraction > plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    positive = sum(item.run.metrics.net_pnl > 0 for item in folds)
    if Decimal(positive) / Decimal(len(folds)) < plan.minimum_positive_fold_fraction:
        reasons.append("POSITIVE_FOLD_FRACTION_BELOW_GATE")
    return reasons
