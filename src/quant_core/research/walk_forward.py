from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from quant_core.config import AppConfig
from quant_core.domain import FrozenModel, _require_utc
from quant_core.governance import EvaluationPlan, EvaluationStage
from quant_core.ids import content_hash, stable_id
from quant_core.research.backtest import (
    BacktestMetrics,
    BacktestRun,
    BacktestTrade,
    ResearchStrategy,
    _mean_lower_bound,
    artifact_hash,
    run_bar_backtest,
)
from quant_core.research.dataset import HistoricalDataset, HistoricalEventDataset
from quant_core.strategy import PriceTrendStrategy

WALK_FORWARD_EVALUATION_SPEC_VERSION = "walk-forward-evaluation-spec-v1"
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
    research_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: WalkForwardPlan

    @classmethod
    def freeze(
        cls,
        *,
        candidate: str,
        dataset: HistoricalDataset,
        event_dataset: HistoricalEventDataset | None,
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
        registered_at=_require_utc(registered_at),
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
    if plan.registered_at > _require_utc(evaluated_at):
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
        or plan.blind_query_budget < 1
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

    _utc_training_start = field_validator("training_start")(_require_utc)
    _utc_training_end = field_validator("training_end")(_require_utc)
    _utc_test_start = field_validator("test_start")(_require_utc)
    _utc_test_end = field_validator("test_end")(_require_utc)

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
            for item in self.folds
        ):
            raise ValueError("walk-forward 折叠数据制品身份不一致")
        return self


def run_walk_forward(
    *,
    dataset: HistoricalDataset,
    event_dataset: HistoricalEventDataset | None = None,
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
