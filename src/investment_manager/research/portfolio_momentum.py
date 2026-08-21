from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.governance.models import (
    EvaluationPlan,
    EvaluationStage,
    FailedExperiment,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, floor_to_step
from investment_manager.market.models import ClosedMarketBar
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.dataset import HistoricalDataset, InstrumentSpec


class PortfolioMomentumPolicy(FrozenModel):
    """One fixed, low-capacity market-portfolio momentum hypothesis."""

    version: Literal["btc-eth-market-tsmom-v1"] = "btc-eth-market-tsmom-v1"
    symbols: tuple[Literal["BTCUSDT", "ETHUSDT"], ...] = ("BTCUSDT", "ETHUSDT")
    formation_days: Literal[28] = 28
    holding_days: Literal[5] = 5
    minimum_threshold_history_days: Literal[252] = 252
    entry_percentile: Decimal = Field(default=Decimal("0.6666666666666667"), gt=0, lt=1)
    maximum_gross_exposure_fraction: Literal[Decimal("0.20")] = Decimal("0.20")
    round_trip_cost_bps: Literal[Decimal("27")] = Decimal("27")
    weighting: Literal["lagged-close-times-volume"] = "lagged-close-times-volume"
    signal_rule: Literal["expanding-nearest-rank-upper-tercile"] = (
        "expanding-nearest-rank-upper-tercile"
    )
    execution_rule: Literal["signal-close-next-open"] = "signal-close-next-open"

    @model_validator(mode="after")
    def symbols_are_exact_and_unique(self):
        if self.symbols != ("BTCUSDT", "ETHUSDT"):
            raise ValueError("v1 组合动量只冻结 BTCUSDT 与 ETHUSDT")
        return self


class PortfolioMomentumLeg(FrozenModel):
    symbol: str
    weight: Decimal = Field(gt=0, le=1)
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    exit_price: Decimal = Field(gt=0)


class PortfolioMomentumTrade(FrozenModel):
    trade_id: str
    signal_at: datetime
    opened_at: datetime
    closed_at: datetime
    formation_return: Decimal
    threshold: Decimal
    entry_notional: Decimal = Field(gt=0)
    gross_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    net_pnl: Decimal
    legs: tuple[PortfolioMomentumLeg, ...] = Field(min_length=1)

    _utc_signal_at = field_validator("signal_at")(require_utc)
    _utc_opened_at = field_validator("opened_at")(require_utc)
    _utc_closed_at = field_validator("closed_at")(require_utc)

    @model_validator(mode="after")
    def trade_reconciles(self):
        if not self.signal_at < self.opened_at < self.closed_at:
            raise ValueError("组合动量交易时间顺序非法")
        if abs(self.gross_pnl - self.modeled_cost - self.net_pnl) > Decimal("0.00000001"):
            raise ValueError("组合动量净损益必须等于毛损益减成本")
        return self


class PortfolioMomentumMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal = Field(gt=0)
    gross_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    net_pnl: Decimal
    return_fraction: Decimal
    simple_annualized_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0, le=1)
    trade_count: int = Field(ge=0)
    active_day_count: int = Field(ge=0)
    win_rate: Decimal | None = Field(default=None, ge=0, le=1)
    profit_factor: Decimal | None = Field(default=None, ge=0)
    average_daily_net_return_bps: Decimal | None = None
    daily_net_return_bps_lower_bound: Decimal | None = None

    @model_validator(mode="after")
    def metrics_reconcile(self):
        tolerance = Decimal("0.00000001")
        if abs(self.ending_equity - self.starting_equity - self.net_pnl) > tolerance:
            raise ValueError("组合动量权益与净损益不一致")
        if abs(self.gross_pnl - self.modeled_cost - self.net_pnl) > tolerance:
            raise ValueError("组合动量成本分解不一致")
        return self


class PortfolioMomentumBacktestRun(FrozenModel):
    version: Literal["portfolio-momentum-backtest-v1"] = "portfolio-momentum-backtest-v1"
    run_id: str
    dataset_ids: tuple[tuple[str, str], ...]
    policy: PortfolioMomentumPolicy
    evaluation_start: datetime
    evaluation_end: datetime
    completed: bool
    assumptions: tuple[str, ...]
    daily_net_returns: tuple[Decimal, ...]
    trades: tuple[PortfolioMomentumTrade, ...]
    metrics: PortfolioMomentumMetrics

    _utc_evaluation_start = field_validator("evaluation_start")(require_utc)
    _utc_evaluation_end = field_validator("evaluation_end")(require_utc)


class PortfolioMomentumWalkForwardPlan(FrozenModel):
    plan_id: str
    training_days: int = Field(default=730, ge=365)
    test_days: int = Field(default=365, ge=90)
    starting_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    minimum_trades: int = Field(default=60, ge=1)
    minimum_profit_factor: Decimal = Field(default=Decimal("1.05"), gt=0)
    minimum_daily_return_bps_lower_bound: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    minimum_positive_fold_fraction: Decimal = Field(
        default=Decimal("0.75"), gt=0, le=1
    )


class PortfolioMomentumFold(FrozenModel):
    fold_id: str
    training_start: datetime
    training_end: datetime
    test_start: datetime
    test_end: datetime
    embargo_days: int = Field(gt=0)
    purge_days: int = Field(gt=0)
    run: PortfolioMomentumBacktestRun

    _utc_training_start = field_validator("training_start")(require_utc)
    _utc_training_end = field_validator("training_end")(require_utc)
    _utc_test_start = field_validator("test_start")(require_utc)
    _utc_test_end = field_validator("test_end")(require_utc)

    @model_validator(mode="after")
    def windows_are_separated(self):
        if not self.training_start < self.training_end < self.test_start < self.test_end:
            raise ValueError("组合动量 walk-forward 时间窗口非法")
        return self


class PortfolioMomentumWalkForwardMetrics(FrozenModel):
    average_annualized_return_fraction: Decimal
    aggregate_net_pnl: Decimal
    aggregate_trade_count: int = Field(ge=0)
    aggregate_profit_factor: Decimal | None = Field(default=None, ge=0)
    daily_net_return_bps_lower_bound: Decimal | None = None
    positive_fold_fraction: Decimal = Field(ge=0, le=1)
    maximum_drawdown_fraction: Decimal = Field(ge=0, le=1)


class PortfolioMomentumWalkForwardResult(FrozenModel):
    version: Literal["portfolio-momentum-walk-forward-v1"] = (
        "portfolio-momentum-walk-forward-v1"
    )
    evaluation_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_ids: tuple[tuple[str, str], ...]
    policy: PortfolioMomentumPolicy
    plan: PortfolioMomentumWalkForwardPlan
    embargo_days: int = Field(gt=0)
    purge_days: int = Field(gt=0)
    folds: tuple[PortfolioMomentumFold, ...] = Field(min_length=1)
    metrics: PortfolioMomentumWalkForwardMetrics
    passed: bool
    reason_codes: tuple[str, ...]


class PortfolioMomentumEvaluationSpec(FrozenModel):
    version: Literal["portfolio-momentum-evaluation-spec-v1"] = (
        "portfolio-momentum-evaluation-spec-v1"
    )
    dataset_ids: tuple[tuple[str, str], ...]
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_environment: tuple[tuple[str, str], ...] = Field(min_length=2)
    history_disposition: Literal["previously-exposed-no-blind-claim"] = (
        "previously-exposed-no-blind-claim"
    )
    policy: PortfolioMomentumPolicy
    plan: PortfolioMomentumWalkForwardPlan

    @model_validator(mode="after")
    def identities_are_canonical(self):
        if self.dataset_ids != tuple(sorted(set(self.dataset_ids))):
            raise ValueError("组合动量数据身份必须唯一并按品种排序")
        if tuple(sorted(set(self.evaluator_environment))) != self.evaluator_environment:
            raise ValueError("组合动量评价环境必须唯一并排序")
        return self


class _WalkForwardEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: PortfolioMomentumWalkForwardResult


class PortfolioMomentumEvaluationCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: PortfolioMomentumWalkForwardResult) -> Path:
        target = self._root / f"{result.evaluation_id}.json"
        if target.exists():
            if self.load(result.evaluation_id) != result:
                raise ValueError("同一组合动量评价 ID 的内容不一致")
            return target
        envelope = _WalkForwardEnvelope(result_hash=content_hash(result), result=result)
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".portfolio-momentum-",
            payload=envelope,
        )

    def load(self, evaluation_id: str) -> PortfolioMomentumWalkForwardResult:
        raw = json.loads(
            (self._root / f"{evaluation_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(
            raw.get("result")
        ):
            raise ValueError("组合动量评价制品哈希不匹配")
        envelope = _WalkForwardEnvelope.model_validate(raw)
        if envelope.result.evaluation_id != evaluation_id:
            raise ValueError("组合动量评价文件名与内容 ID 不一致")
        return envelope.result


@dataclass(frozen=True, slots=True)
class _OpenCohort:
    signal_index: int
    entry_index: int
    exit_index: int
    formation_return: Decimal
    threshold: Decimal
    entry_notional: Decimal
    modeled_cost: Decimal
    weights: tuple[tuple[str, Decimal], ...]
    quantities: tuple[tuple[str, Decimal], ...]
    entry_prices: tuple[tuple[str, Decimal], ...]


def dataset_identities(
    datasets: tuple[HistoricalDataset, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((item.manifest.symbol, item.manifest.dataset_id) for item in datasets)
    )


def current_portfolio_momentum_environment() -> tuple[tuple[str, str], ...]:
    return (
        ("pydantic", distribution_version("pydantic")),
        ("python", platform.python_version()),
    )


def build_portfolio_momentum_evaluation_plan(
    *,
    spec: PortfolioMomentumEvaluationSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    return EvaluationPlan(
        plan_id=spec.plan.plan_id,
        registered_at=require_utc(registered_at),
        base_manifest_id=base_manifest_id,
        primary_metric="daily_net_return_bps_lower_bound",
        minimum_sample_size=spec.plan.minimum_trades,
        hard_guardrails=(
            "NET_PNL_POSITIVE_AFTER_COSTS",
            "DAILY_NET_RETURN_LOWER_BOUND_POSITIVE",
            "PROFIT_FACTOR_WITHIN_LIMIT",
            "POSITIVE_FOLD_FRACTION_WITHIN_LIMIT",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
        ),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
        ),
        fixed_regression_suite_version="investment-manager-portfolio-momentum-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=0,
    )


def validate_portfolio_momentum_evaluation_plan(
    *,
    spec: PortfolioMomentumEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
    evaluated_at: datetime,
    evaluator_code_version: str,
    evaluator_environment: tuple[tuple[str, str], ...],
) -> None:
    if evaluator_code_version != spec.evaluator_code_version:
        raise ValueError("组合动量必须使用预登记的精确评价代码版本")
    if evaluator_environment != spec.evaluator_environment:
        raise ValueError("组合动量必须使用预登记的精确评价依赖环境")
    expected = build_portfolio_momentum_evaluation_plan(
        spec=spec,
        base_manifest_id=champion_manifest_id,
        registered_at=plan.registered_at,
    )
    if require_utc(evaluated_at) < plan.registered_at:
        raise ValueError("组合动量评价不能早于预登记")
    if plan != expected:
        raise ValueError("组合动量 EvaluationPlan 与完整预登记合同不一致")


def failed_portfolio_momentum_experiment(
    result: PortfolioMomentumWalkForwardResult, *, rejected_at: datetime
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的组合动量评价不能登记为失败实验")
    hypothesis = (
        "BTCUSDT 与 ETHUSDT 的固定成交额加权 28 日市场组合动量，在 20% 总敞口、"
        "5 日交错持有和 27bp 往返成本下满足费用后收益、稳定性与回撤门槛"
    )
    return FailedExperiment(
        experiment_id=stable_id(
            "failed_portfolio_momentum", result.evaluation_id
        ),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(f"hypothesis:{hypothesis}", result.evaluation_id),
        rejected_at=require_utc(rejected_at),
        reason_codes=("PORTFOLIO_MOMENTUM_WALK_FORWARD_FAILED", *result.reason_codes),
    )


def run_portfolio_momentum_backtest(
    *,
    datasets: tuple[HistoricalDataset, ...],
    policy: PortfolioMomentumPolicy,
    starting_equity: Decimal,
    evaluation_start: datetime,
    evaluation_end: datetime,
) -> PortfolioMomentumBacktestRun:
    """Replay fixed BTC/ETH cohorts using only information visible at each close."""

    evaluation_start = require_utc(evaluation_start)
    evaluation_end = require_utc(evaluation_end)
    bars_by_symbol = _aligned_bars(datasets, policy)
    instruments = {item.manifest.symbol: item.manifest.instrument for item in datasets}
    symbols = policy.symbols
    bars = bars_by_symbol[symbols[0]]
    if evaluation_start >= evaluation_end or starting_equity <= 0:
        raise ValueError("组合动量评价窗口与初始权益必须有效")
    index_by_open = {bar.open_time: index for index, bar in enumerate(bars)}
    try:
        start_index = index_by_open[evaluation_start]
        end_index = index_by_open[evaluation_end]
    except KeyError as exc:
        raise ValueError("组合动量评价边界必须与日线开盘严格对齐") from exc
    if end_index - start_index <= policy.holding_days + 1:
        raise ValueError("组合动量评价窗口不足以完成一个持有周期")

    scores = _formation_scores(bars_by_symbol, policy)
    weights_by_index = _visible_weights(bars_by_symbol, symbols)
    cash = starting_equity
    open_cohorts: list[_OpenCohort] = []
    trades: list[PortfolioMomentumTrade] = []
    daily_returns: list[Decimal] = []
    previous_close_equity = starting_equity
    high_water = starting_equity
    maximum_drawdown = Decimal("0")
    active_day_count = 0

    for index in range(start_index, end_index):
        open_prices = {symbol: bars_by_symbol[symbol][index].open for symbol in symbols}
        remaining: list[_OpenCohort] = []
        for cohort in open_cohorts:
            if cohort.exit_index != index:
                remaining.append(cohort)
                continue
            proceeds = sum(
                quantity * open_prices[symbol] for symbol, quantity in cohort.quantities
            )
            cash += proceeds
            entry_prices = dict(cohort.entry_prices)
            weights = dict(cohort.weights)
            legs = tuple(
                PortfolioMomentumLeg(
                    symbol=symbol,
                    weight=weights[symbol],
                    quantity=quantity,
                    entry_price=entry_prices[symbol],
                    exit_price=open_prices[symbol],
                )
                for symbol, quantity in cohort.quantities
            )
            gross = proceeds - cohort.entry_notional
            trade_id = stable_id(
                "portfolio_momentum_trade",
                dataset_identities(datasets),
                cohort.signal_index,
                cohort.entry_index,
                cohort.exit_index,
                cohort.quantities,
            )
            trades.append(
                PortfolioMomentumTrade(
                    trade_id=trade_id,
                    signal_at=bars[cohort.signal_index].close_time,
                    opened_at=bars[cohort.entry_index].open_time,
                    closed_at=bars[cohort.exit_index].open_time,
                    formation_return=cohort.formation_return,
                    threshold=cohort.threshold,
                    entry_notional=cohort.entry_notional,
                    gross_pnl=gross,
                    modeled_cost=cohort.modeled_cost,
                    net_pnl=gross - cohort.modeled_cost,
                    legs=legs,
                )
            )
        open_cohorts = remaining

        signal_index = index - 1
        last_signal_index = end_index - policy.holding_days - 2
        if signal_index >= start_index and signal_index <= last_signal_index:
            score = scores[signal_index]
            history = tuple(item for item in scores[:signal_index] if item is not None)
            if score is not None and len(history) >= policy.minimum_threshold_history_days:
                threshold = _nearest_rank(history, policy.entry_percentile)
                if score > threshold:
                    marked_open_equity = cash + _marked_value(
                        open_cohorts, open_prices
                    )
                    cohort_budget = (
                        marked_open_equity
                        * policy.maximum_gross_exposure_fraction
                        / Decimal(policy.holding_days)
                    )
                    maximum_affordable = cash / (
                        Decimal("1") + policy.round_trip_cost_bps / Decimal("10000")
                    )
                    cohort = _open_cohort(
                        bars_by_symbol=bars_by_symbol,
                        instruments=instruments,
                        weights=weights_by_index[signal_index],
                        policy=policy,
                        signal_index=signal_index,
                        entry_index=index,
                        target_notional=min(cohort_budget, maximum_affordable),
                        formation_return=score,
                        threshold=threshold,
                    )
                    if cohort is not None:
                        cash -= cohort.entry_notional + cohort.modeled_cost
                        open_cohorts.append(cohort)

        open_equity = cash + _marked_value(open_cohorts, open_prices)
        high_water = max(high_water, open_equity)
        maximum_drawdown = max(
            maximum_drawdown,
            (high_water - open_equity) / high_water,
        )

        close_prices = {symbol: bars_by_symbol[symbol][index].close for symbol in symbols}
        close_equity = cash + _marked_value(open_cohorts, close_prices)
        if open_cohorts:
            active_day_count += 1
        daily_returns.append(close_equity / previous_close_equity - Decimal("1"))
        previous_close_equity = close_equity
        high_water = max(high_water, close_equity)
        maximum_drawdown = max(
            maximum_drawdown,
            (high_water - close_equity) / high_water,
        )

    completed = not open_cohorts
    gross_pnl = sum((item.gross_pnl for item in trades), Decimal("0"))
    modeled_cost = sum((item.modeled_cost for item in trades), Decimal("0"))
    marked_net_pnl = previous_close_equity - starting_equity
    ledger_net_pnl = gross_pnl - modeled_cost
    if completed and abs(ledger_net_pnl - marked_net_pnl) > Decimal("0.00000001"):
        raise ValueError(
            "组合动量交易账本与权益曲线不一致："
            f"trades={ledger_net_pnl}, equity={marked_net_pnl}"
        )
    net_pnl = ledger_net_pnl if completed else marked_net_pnl
    ending_equity = starting_equity + net_pnl
    wins = tuple(item.net_pnl for item in trades if item.net_pnl > 0)
    losses = tuple(-item.net_pnl for item in trades if item.net_pnl < 0)
    day_count = end_index - start_index
    daily_bps = tuple(item * Decimal("10000") for item in daily_returns)
    metrics = PortfolioMomentumMetrics(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        gross_pnl=gross_pnl,
        modeled_cost=modeled_cost,
        net_pnl=net_pnl,
        return_fraction=net_pnl / starting_equity,
        simple_annualized_return_fraction=(net_pnl / starting_equity)
        * Decimal("365")
        / Decimal(day_count),
        maximum_drawdown_fraction=maximum_drawdown,
        trade_count=len(trades),
        active_day_count=active_day_count,
        win_rate=(Decimal(len(wins)) / Decimal(len(trades)) if trades else None),
        profit_factor=(
            sum(wins, Decimal("0")) / sum(losses, Decimal("0")) if losses else None
        ),
        average_daily_net_return_bps=(
            sum(daily_bps, Decimal("0")) / Decimal(len(daily_bps))
            if daily_bps
            else None
        ),
        daily_net_return_bps_lower_bound=_conservative_newey_west_lower_bound(
            daily_bps,
            z=Decimal("1.96"),
            lag=policy.holding_days,
        ),
    )
    identities = dataset_identities(datasets)
    run_id = stable_id(
        "portfolio_momentum_backtest",
        identities,
        policy,
        starting_equity,
        evaluation_start,
        evaluation_end,
        [item.trade_id for item in trades],
    )
    return PortfolioMomentumBacktestRun(
        run_id=run_id,
        dataset_ids=identities,
        policy=policy,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        completed=completed,
        assumptions=(
            "FIXED_BTC_ETH_POINT_IN_TIME_UNIVERSE",
            "LAGGED_CLOSE_TIMES_VOLUME_PORTFOLIO_WEIGHTS",
            "28_DAY_COMPOUNDED_MARKET_PORTFOLIO_RETURN",
            "EXPANDING_PRIOR_SCORE_UPPER_TERCILE",
            "SIGNAL_AT_CLOSE_ENTER_NEXT_OPEN",
            "FIVE_DAILY_STAGGERED_COHORTS",
            "MAXIMUM_GROSS_EXPOSURE_20_PERCENT",
            "ROUND_TRIP_COST_27_BPS_CHARGED_AT_ENTRY",
            "NO_CODEX_REPLAY",
        ),
        daily_net_returns=tuple(daily_returns),
        trades=tuple(trades),
        metrics=metrics,
    )


def run_portfolio_momentum_walk_forward(
    *,
    datasets: tuple[HistoricalDataset, ...],
    policy: PortfolioMomentumPolicy,
    plan: PortfolioMomentumWalkForwardPlan,
    evaluation_spec_hash: str,
) -> PortfolioMomentumWalkForwardResult:
    bars_by_symbol = _aligned_bars(datasets, policy)
    bars = bars_by_symbol[policy.symbols[0]]
    embargo_days = policy.holding_days + 1
    purge_days = policy.holding_days + 1
    folds: list[PortfolioMomentumFold] = []
    training_end = plan.training_days
    while True:
        test_start = training_end + embargo_days
        test_end = test_start + plan.test_days
        if test_end >= len(bars):
            break
        run = run_portfolio_momentum_backtest(
            datasets=datasets,
            policy=policy,
            starting_equity=plan.starting_equity,
            evaluation_start=bars[test_start].open_time,
            evaluation_end=bars[test_end].open_time,
        )
        folds.append(
            PortfolioMomentumFold(
                fold_id=stable_id(
                    "portfolio_momentum_fold",
                    plan.plan_id,
                    len(folds),
                    bars[0].open_time,
                    bars[training_end - 1].close_time,
                    run.run_id,
                ),
                training_start=bars[0].open_time,
                training_end=bars[training_end - 1].close_time,
                test_start=bars[test_start].open_time,
                test_end=bars[test_end].open_time,
                embargo_days=embargo_days,
                purge_days=purge_days,
                run=run,
            )
        )
        training_end = test_end
    if not folds:
        required = plan.training_days + embargo_days + plan.test_days + 1
        raise ValueError(f"组合动量数据不足以形成 walk-forward；至少需要 {required} 日")

    trades = tuple(trade for fold in folds for trade in fold.run.trades)
    wins = tuple(item.net_pnl for item in trades if item.net_pnl > 0)
    losses = tuple(-item.net_pnl for item in trades if item.net_pnl < 0)
    daily_bps = tuple(
        value * Decimal("10000")
        for fold in folds
        for value in fold.run.daily_net_returns
    )
    positive_folds = sum(fold.run.metrics.net_pnl > 0 for fold in folds)
    metrics = PortfolioMomentumWalkForwardMetrics(
        average_annualized_return_fraction=sum(
            (fold.run.metrics.simple_annualized_return_fraction for fold in folds),
            Decimal("0"),
        )
        / Decimal(len(folds)),
        aggregate_net_pnl=sum(
            (fold.run.metrics.net_pnl for fold in folds), Decimal("0")
        ),
        aggregate_trade_count=len(trades),
        aggregate_profit_factor=(
            sum(wins, Decimal("0")) / sum(losses, Decimal("0")) if losses else None
        ),
        daily_net_return_bps_lower_bound=_conservative_newey_west_lower_bound(
            daily_bps,
            z=Decimal("1.96"),
            lag=policy.holding_days,
        ),
        positive_fold_fraction=Decimal(positive_folds) / Decimal(len(folds)),
        maximum_drawdown_fraction=max(
            fold.run.metrics.maximum_drawdown_fraction for fold in folds
        ),
    )
    reasons = _walk_forward_reasons(folds, metrics, plan)
    identities = dataset_identities(datasets)
    evaluation_id = stable_id(
        "portfolio_momentum_walk_forward",
        evaluation_spec_hash,
        identities,
        policy,
        plan,
        [fold.run.run_id for fold in folds],
    )
    return PortfolioMomentumWalkForwardResult(
        evaluation_id=evaluation_id,
        evaluation_spec_hash=evaluation_spec_hash,
        dataset_ids=identities,
        policy=policy,
        plan=plan,
        embargo_days=embargo_days,
        purge_days=purge_days,
        folds=tuple(folds),
        metrics=metrics,
        passed=not reasons,
        reason_codes=tuple(reasons) or ("ALL_PREREGISTERED_GATES_PASSED",),
    )


def _aligned_bars(
    datasets: tuple[HistoricalDataset, ...], policy: PortfolioMomentumPolicy
) -> dict[str, tuple[ClosedMarketBar, ...]]:
    by_symbol = {item.manifest.symbol: item for item in datasets}
    if tuple(sorted(by_symbol)) != tuple(sorted(policy.symbols)) or len(datasets) != len(
        policy.symbols
    ):
        raise ValueError("组合动量必须提供精确的 BTCUSDT 与 ETHUSDT 数据集")
    reference = by_symbol[policy.symbols[0]].bars
    if any(item.manifest.interval != "1d" for item in datasets):
        raise ValueError("组合动量只接受冻结的 1d 数据")
    if any(item.manifest.instrument.quote_asset != "USDT" for item in datasets):
        raise ValueError("组合动量数据必须使用同一 USDT 报价")
    for symbol in policy.symbols[1:]:
        dataset = by_symbol[symbol]
        if len(dataset.bars) != len(reference) or any(
            (left.open_time, left.close_time) != (right.open_time, right.close_time)
            for left, right in zip(reference, dataset.bars, strict=True)
        ):
            raise ValueError("组合动量多资产日线必须逐根严格对齐")
    return {symbol: by_symbol[symbol].bars for symbol in policy.symbols}


def _formation_scores(
    bars_by_symbol: dict[str, tuple[ClosedMarketBar, ...]],
    policy: PortfolioMomentumPolicy,
) -> tuple[Decimal | None, ...]:
    symbols = policy.symbols
    count = len(bars_by_symbol[symbols[0]])
    portfolio_returns: list[Decimal | None] = [None]
    for index in range(1, count):
        turnovers = tuple(
            bars_by_symbol[symbol][index - 1].close
            * bars_by_symbol[symbol][index - 1].volume
            for symbol in symbols
        )
        total = sum(turnovers, Decimal("0"))
        if total <= 0:
            raise ValueError("组合动量权重代理成交额必须为正")
        portfolio_returns.append(
            sum(
                (
                    turnover
                    / total
                    * (
                        bars_by_symbol[symbol][index].close
                        / bars_by_symbol[symbol][index - 1].close
                        - Decimal("1")
                    )
                    for symbol, turnover in zip(symbols, turnovers, strict=True)
                ),
                Decimal("0"),
            )
        )
    scores: list[Decimal | None] = [None] * count
    for index in range(policy.formation_days, count):
        compounded = Decimal("1")
        for daily_return in portfolio_returns[
            index - policy.formation_days + 1 : index + 1
        ]:
            if daily_return is None:
                raise ValueError("组合动量形成窗口存在缺失收益")
            compounded *= Decimal("1") + daily_return
        scores[index] = compounded - Decimal("1")
    return tuple(scores)


def _visible_weights(
    bars_by_symbol: dict[str, tuple[ClosedMarketBar, ...]], symbols: tuple[str, ...]
) -> tuple[tuple[tuple[str, Decimal], ...], ...]:
    result = []
    for index in range(len(bars_by_symbol[symbols[0]])):
        values = tuple(
            bars_by_symbol[symbol][index].close * bars_by_symbol[symbol][index].volume
            for symbol in symbols
        )
        total = sum(values, Decimal("0"))
        if total <= 0:
            raise ValueError("组合动量可见权重代理成交额必须为正")
        result.append(
            tuple(
                (symbol, value / total)
                for symbol, value in zip(symbols, values, strict=True)
            )
        )
    return tuple(result)


def _nearest_rank(values: tuple[Decimal, ...], percentile: Decimal) -> Decimal:
    ordered = sorted(values)
    rank = (percentile * Decimal(len(ordered))).to_integral_value(
        rounding=ROUND_CEILING
    )
    return ordered[max(0, int(rank) - 1)]


def _open_cohort(
    *,
    bars_by_symbol: dict[str, tuple[ClosedMarketBar, ...]],
    instruments: dict[str, InstrumentSpec],
    weights: tuple[tuple[str, Decimal], ...],
    policy: PortfolioMomentumPolicy,
    signal_index: int,
    entry_index: int,
    target_notional: Decimal,
    formation_return: Decimal,
    threshold: Decimal,
) -> _OpenCohort | None:
    quantities: list[tuple[str, Decimal]] = []
    entry_prices: list[tuple[str, Decimal]] = []
    accepted_weights: list[tuple[str, Decimal]] = []
    for symbol, weight in weights:
        dataset_bar = bars_by_symbol[symbol][entry_index]
        price = dataset_bar.open
        raw_quantity = target_notional * weight / price
        instrument = instruments[symbol]
        quantity = floor_to_step(raw_quantity, instrument.quantity_increment)
        if (
            quantity < instrument.minimum_quantity
            or quantity > instrument.maximum_quantity
            or quantity * price < instrument.minimum_notional
        ):
            continue
        quantities.append((symbol, quantity))
        entry_prices.append((symbol, price))
        accepted_weights.append((symbol, weight))
    if not quantities:
        return None
    entry_notional = sum(
        quantity * dict(entry_prices)[symbol] for symbol, quantity in quantities
    )
    modeled_cost = entry_notional * policy.round_trip_cost_bps / Decimal("10000")
    return _OpenCohort(
        signal_index=signal_index,
        entry_index=entry_index,
        exit_index=entry_index + policy.holding_days,
        formation_return=formation_return,
        threshold=threshold,
        entry_notional=entry_notional,
        modeled_cost=modeled_cost,
        weights=tuple(accepted_weights),
        quantities=tuple(quantities),
        entry_prices=tuple(entry_prices),
    )


def _marked_value(
    cohorts: list[_OpenCohort], prices: dict[str, Decimal]
) -> Decimal:
    return sum(
        (
            quantity * prices[symbol]
            for cohort in cohorts
            for symbol, quantity in cohort.quantities
        ),
        Decimal("0"),
    )


def _conservative_newey_west_lower_bound(
    values: tuple[Decimal, ...], *, z: Decimal, lag: int
) -> Decimal | None:
    if len(values) <= lag:
        return None
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    residuals = tuple(item - mean for item in values)
    gamma_zero = sum((item**2 for item in residuals), Decimal("0")) / count
    long_run_variance = gamma_zero
    for offset in range(1, lag + 1):
        covariance = sum(
            (
                residuals[index] * residuals[index - offset]
                for index in range(offset, len(residuals))
            ),
            Decimal("0"),
        ) / count
        weight = Decimal("1") - Decimal(offset) / Decimal(lag + 1)
        long_run_variance += Decimal("2") * weight * covariance
    conservative_variance = max(gamma_zero, long_run_variance, Decimal("0"))
    return mean - z * (conservative_variance / count).sqrt()


def _walk_forward_reasons(
    folds: list[PortfolioMomentumFold],
    metrics: PortfolioMomentumWalkForwardMetrics,
    plan: PortfolioMomentumWalkForwardPlan,
) -> list[str]:
    reasons: list[str] = []
    if not all(fold.run.completed for fold in folds):
        reasons.append("INCOMPLETE_BACKTEST")
    if metrics.aggregate_trade_count < plan.minimum_trades:
        reasons.append("MINIMUM_TRADES_NOT_MET")
    if metrics.aggregate_net_pnl <= 0:
        reasons.append("NET_PNL_NOT_POSITIVE")
    if (
        metrics.daily_net_return_bps_lower_bound is None
        or metrics.daily_net_return_bps_lower_bound
        <= plan.minimum_daily_return_bps_lower_bound
    ):
        reasons.append("DAILY_NET_RETURN_LOWER_BOUND_NOT_POSITIVE")
    if (
        metrics.aggregate_profit_factor is None
        or metrics.aggregate_profit_factor < plan.minimum_profit_factor
    ):
        reasons.append("PROFIT_FACTOR_BELOW_GATE")
    if metrics.positive_fold_fraction < plan.minimum_positive_fold_fraction:
        reasons.append("POSITIVE_FOLD_FRACTION_BELOW_GATE")
    if metrics.maximum_drawdown_fraction > plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    return reasons
