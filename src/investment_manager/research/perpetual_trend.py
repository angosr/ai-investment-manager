from __future__ import annotations

import json
import platform
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.carry import (
    CarryFundingSettlement,
    HistoricalCarryDataset,
    HistoricalCarryDatasetManifest,
)


class PerpetualTrendPolicy(FrozenModel):
    """One fixed, symmetric USD-M trend hypothesis; this is not a parameter grid."""

    version: Literal["symmetric-perpetual-trend-risk-v1"] = (
        "symmetric-perpetual-trend-risk-v1"
    )
    family: Literal["symmetric-directional-perpetual-trend"] = (
        "symmetric-directional-perpetual-trend"
    )
    momentum_lookback_days: Literal[28] = 28
    regime_moving_average_days: Literal[200] = 200
    volatility_lookback_days: Literal[30] = 30
    annualization_days: Literal[365] = 365
    target_annual_volatility_fraction: Decimal = Decimal("0.04")
    maximum_gross_exposure_fraction: Decimal = Decimal("0.10")
    minimum_rebalance_notional: Decimal = Decimal("25")
    one_way_cost_bps: Decimal = Decimal("10")
    margin_budget_fraction: Decimal = Decimal("0.30")
    maintenance_margin_fraction: Decimal = Decimal("0.10")

    @model_validator(mode="after")
    def economic_assumptions_are_fixed(self):
        expected = {
            "target_annual_volatility_fraction": Decimal("0.04"),
            "maximum_gross_exposure_fraction": Decimal("0.10"),
            "minimum_rebalance_notional": Decimal("25"),
            "one_way_cost_bps": Decimal("10"),
            "margin_budget_fraction": Decimal("0.30"),
            "maintenance_margin_fraction": Decimal("0.10"),
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise ValueError("永续趋势经济假设已经冻结，禁止开发期参数择优")
        return self


class PerpetualTrendWalkForwardPlan(FrozenModel):
    plan_id: str = Field(min_length=1)
    development_end: datetime = datetime(2025, 8, 1, tzinfo=UTC)
    starting_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    minimum_weekly_samples: int = Field(default=100, ge=30)
    minimum_weekly_return_lower_bound: Decimal = Decimal("0")
    minimum_annualized_return_fraction: Decimal = Decimal("0")
    minimum_positive_fold_fraction: Decimal = Field(
        default=Decimal("0.75"), gt=0, le=1
    )
    maximum_drawdown_fraction: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=1
    )

    _utc_development_end = field_validator("development_end")(require_utc)


class PerpetualTrendEvaluationSpec(FrozenModel):
    version: Literal["perpetual-trend-evaluation-spec-v1"] = (
        "perpetual-trend-evaluation-spec-v1"
    )
    carry_dataset_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_environment: tuple[tuple[str, str], ...] = Field(min_length=2)
    policy: PerpetualTrendPolicy
    plan: PerpetualTrendWalkForwardPlan

    @model_validator(mode="after")
    def contract_is_fixed_and_sorted(self):
        if tuple(sorted(set(self.evaluator_environment))) != self.evaluator_environment:
            raise ValueError("永续趋势评价环境必须唯一且有序")
        if self.symbol != "ETHUSDT":
            raise ValueError("首个冻结候选只允许 ETHUSDT，禁止开发期跨品种择优")
        if self.plan.development_end != datetime(2025, 8, 1, tzinfo=UTC):
            raise ValueError("永续趋势开发截止点已冻结为 2025-08-01")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        manifest: HistoricalCarryDatasetManifest,
        evaluator_code_version: str,
        evaluator_environment: tuple[tuple[str, str], ...],
        policy: PerpetualTrendPolicy,
        plan: PerpetualTrendWalkForwardPlan,
    ) -> PerpetualTrendEvaluationSpec:
        if manifest.symbol != "ETHUSDT":
            raise ValueError("冻结的方向性永续候选必须是 ETHUSDT")
        if not manifest.requested_start < plan.development_end < manifest.requested_end:
            raise ValueError("carry 数据必须同时覆盖开发区与已消费的历史尾窗")
        return cls(
            carry_dataset_id=manifest.dataset_id,
            symbol=manifest.symbol,
            evaluator_code_version=evaluator_code_version,
            evaluator_environment=evaluator_environment,
            policy=policy,
            plan=plan,
        )


class PerpetualTrendRunMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal
    net_pnl: Decimal
    price_pnl: Decimal
    funding_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    return_fraction: Decimal
    simple_annualized_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    rebalance_count: int = Field(ge=0)
    long_days: int = Field(ge=0)
    short_days: int = Field(ge=0)
    cash_days: int = Field(ge=0)
    liquidated: bool

    @model_validator(mode="after")
    def pnl_reconciles(self):
        if self.ending_equity - self.starting_equity != self.net_pnl:
            raise ValueError("永续趋势权益与净损益不一致")
        if self.price_pnl + self.funding_pnl - self.modeled_cost != self.net_pnl:
            raise ValueError("永续趋势价格、资金费与成本无法核对")
        return self


class PerpetualTrendYearRun(FrozenModel):
    run_id: str
    year: int
    start: datetime
    end: datetime
    completed: bool
    reason_codes: tuple[str, ...]
    weekly_returns: tuple[Decimal, ...]
    metrics: PerpetualTrendRunMetrics

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)


class PerpetualTrendWalkForwardMetrics(FrozenModel):
    fold_count: int = Field(ge=1)
    weekly_sample_count: int = Field(ge=0)
    average_weekly_return_fraction: Decimal | None = None
    weekly_return_lower_bound: Decimal | None = None
    average_annualized_return_fraction: Decimal
    positive_fold_fraction: Decimal = Field(ge=0, le=1)
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    aggregate_net_pnl: Decimal
    aggregate_price_pnl: Decimal
    aggregate_funding_pnl: Decimal
    aggregate_modeled_cost: Decimal = Field(ge=0)
    rebalance_count: int = Field(ge=0)

    @model_validator(mode="after")
    def aggregate_pnl_reconciles(self):
        if (
            self.aggregate_price_pnl
            + self.aggregate_funding_pnl
            - self.aggregate_modeled_cost
            != self.aggregate_net_pnl
        ):
            raise ValueError("永续趋势分折汇总损益无法核对")
        return self


class PerpetualTrendWalkForwardResult(FrozenModel):
    version: Literal["perpetual-trend-walk-forward-v1"] = (
        "perpetual-trend-walk-forward-v1"
    )
    evaluation_id: str
    carry_dataset_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: PerpetualTrendPolicy
    plan: PerpetualTrendWalkForwardPlan
    folds: tuple[PerpetualTrendYearRun, ...] = Field(min_length=1)
    metrics: PerpetualTrendWalkForwardMetrics
    passed: bool
    reason_codes: tuple[str, ...]


class PerpetualTrendEvaluationEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: PerpetualTrendWalkForwardResult


class PerpetualTrendEvaluationCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: PerpetualTrendWalkForwardResult) -> Path:
        target = self._root / f"{result.evaluation_id}.json"
        if target.exists():
            if self.load(result.evaluation_id) != result:
                raise ValueError("同一永续趋势评价 ID 的内容不一致")
            return target
        envelope = PerpetualTrendEvaluationEnvelope(
            result_hash=content_hash(result), result=result
        )
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".perpetual-trend-evaluation-",
            payload=envelope,
        )

    def load(self, evaluation_id: str) -> PerpetualTrendWalkForwardResult:
        raw = json.loads(
            (self._root / f"{evaluation_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(
            raw.get("result")
        ):
            raise ValueError("永续趋势评价制品内容哈希不匹配")
        envelope = PerpetualTrendEvaluationEnvelope.model_validate(raw)
        if envelope.result.evaluation_id != evaluation_id:
            raise ValueError("永续趋势评价文件名与内容 ID 不一致")
        return envelope.result


def current_perpetual_trend_evaluator_environment() -> tuple[tuple[str, str], ...]:
    return (
        ("pydantic", distribution_version("pydantic")),
        ("python", platform.python_version()),
    )


def build_perpetual_trend_evaluation_plan(
    *,
    spec: PerpetualTrendEvaluationSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    return EvaluationPlan(
        plan_id=spec.plan.plan_id,
        registered_at=require_utc(registered_at),
        base_manifest_id=base_manifest_id,
        primary_metric="weekly_return_lower_bound",
        minimum_sample_size=spec.plan.minimum_weekly_samples,
        hard_guardrails=(
            "WEEKLY_RETURN_LOWER_BOUND_POSITIVE",
            "ANNUALIZED_RETURN_POSITIVE",
            "POSITIVE_FOLD_FRACTION_WITHIN_LIMIT",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
            "MARGIN_BUFFER_POSITIVE",
        ),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
        ),
        fixed_regression_suite_version="perpetual-trend-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=0,
    )


def validate_perpetual_trend_evaluation_plan(
    *,
    spec: PerpetualTrendEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
    evaluated_at: datetime,
    evaluator_code_version: str,
    evaluator_environment: tuple[tuple[str, str], ...],
) -> None:
    if evaluator_code_version != spec.evaluator_code_version:
        raise ValueError("永续趋势必须使用预登记的精确评价代码版本")
    if evaluator_environment != spec.evaluator_environment:
        raise ValueError("永续趋势必须使用预登记的精确评价依赖环境")
    if plan.plan_id != spec.plan.plan_id:
        raise ValueError("永续趋势评价与预登记计划 ID 不一致")
    if plan.registered_at > require_utc(evaluated_at):
        raise ValueError("永续趋势评价不能早于计划预登记时间")
    if plan.base_manifest_id != champion_manifest_id:
        raise ValueError("永续趋势评价计划不属于当前 Champion")
    if plan.candidate_spec_hash != content_hash(spec):
        raise ValueError("永续趋势数据、策略、成本或门禁与预登记规格不一致")
    if plan.minimum_sample_size != spec.plan.minimum_weekly_samples:
        raise ValueError("永续趋势周样本门槛与预登记计划不一致")
    required = {
        EvaluationStage.STATIC,
        EvaluationStage.FIXED_REGRESSION,
        EvaluationStage.WALK_FORWARD,
    }
    if not required.issubset(plan.required_stages) or plan.blind_query_budget != 0:
        raise ValueError("永续趋势计划不得读取已消费盲区或缺少必要评价阶段")


def run_perpetual_trend_walk_forward(
    *,
    carry_dataset: HistoricalCarryDataset,
    spec: PerpetualTrendEvaluationSpec,
) -> PerpetualTrendWalkForwardResult:
    if carry_dataset.manifest.dataset_id != spec.carry_dataset_id:
        raise ValueError("永续趋势 carry 数据与预登记规格不一致")
    if carry_dataset.manifest.symbol != spec.symbol:
        raise ValueError("永续趋势品种与预登记规格不一致")
    days = carry_dataset.days
    development_indexes = [
        index
        for index, day in enumerate(days)
        if day.close_time < spec.plan.development_end
    ]
    by_year: dict[int, list[int]] = defaultdict(list)
    for index in development_indexes:
        by_year[days[index].open_time.year].append(index)
    warmup = max(
        spec.policy.regime_moving_average_days,
        spec.policy.volatility_lookback_days + 1,
        spec.policy.momentum_lookback_days + 1,
    )
    folds: list[PerpetualTrendYearRun] = []
    for year, indexes in sorted(by_year.items()):
        if indexes[0] < warmup or not _is_complete_utc_year(days, indexes, year):
            continue
        folds.append(
            _run_year(
                carry_dataset=carry_dataset,
                indexes=tuple(indexes),
                year=year,
                policy=spec.policy,
                starting_equity=spec.plan.starting_equity,
            )
        )
    if len(folds) < 3:
        raise ValueError("永续趋势开发区不足三个带完整 warm-up 的 UTC 年折")

    weekly_returns = tuple(
        value for fold in folds for value in fold.weekly_returns
    )
    annualized = tuple(
        fold.metrics.simple_annualized_return_fraction for fold in folds
    )
    metrics = PerpetualTrendWalkForwardMetrics(
        fold_count=len(folds),
        weekly_sample_count=len(weekly_returns),
        average_weekly_return_fraction=(
            _mean(weekly_returns) if weekly_returns else None
        ),
        weekly_return_lower_bound=(
            _mean_lower_bound(weekly_returns) if len(weekly_returns) >= 2 else None
        ),
        average_annualized_return_fraction=_mean(annualized),
        positive_fold_fraction=(
            Decimal(sum(value > 0 for value in annualized)) / Decimal(len(annualized))
        ),
        maximum_drawdown_fraction=max(
            fold.metrics.maximum_drawdown_fraction for fold in folds
        ),
        minimum_margin_buffer_fraction=min(
            fold.metrics.minimum_margin_buffer_fraction for fold in folds
        ),
        aggregate_net_pnl=sum(
            (fold.metrics.net_pnl for fold in folds), Decimal("0")
        ),
        aggregate_price_pnl=sum(
            (fold.metrics.price_pnl for fold in folds), Decimal("0")
        ),
        aggregate_funding_pnl=sum(
            (fold.metrics.funding_pnl for fold in folds), Decimal("0")
        ),
        aggregate_modeled_cost=sum(
            (fold.metrics.modeled_cost for fold in folds), Decimal("0")
        ),
        rebalance_count=sum(fold.metrics.rebalance_count for fold in folds),
    )
    reasons: list[str] = []
    if any(not fold.completed for fold in folds):
        reasons.append("LIQUIDATION_BOUND_BREACHED")
    if metrics.weekly_sample_count < spec.plan.minimum_weekly_samples:
        reasons.append("MINIMUM_WEEKLY_SAMPLES_NOT_MET")
    if (
        metrics.weekly_return_lower_bound is None
        or metrics.weekly_return_lower_bound
        <= spec.plan.minimum_weekly_return_lower_bound
    ):
        reasons.append("WEEKLY_RETURN_LOWER_BOUND_NOT_POSITIVE")
    if (
        metrics.average_annualized_return_fraction
        <= spec.plan.minimum_annualized_return_fraction
    ):
        reasons.append("ANNUALIZED_RETURN_NOT_POSITIVE")
    if metrics.positive_fold_fraction < spec.plan.minimum_positive_fold_fraction:
        reasons.append("POSITIVE_FOLD_FRACTION_BELOW_GATE")
    if metrics.maximum_drawdown_fraction > spec.plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if metrics.minimum_margin_buffer_fraction <= 0:
        reasons.append("MARGIN_BUFFER_NOT_POSITIVE")
    evaluation_spec_hash = content_hash(spec)
    evaluation_id = stable_id(
        "perpetual_trend_walk_forward",
        spec.carry_dataset_id,
        evaluation_spec_hash,
        tuple(fold.run_id for fold in folds),
        metrics,
    )
    return PerpetualTrendWalkForwardResult(
        evaluation_id=evaluation_id,
        carry_dataset_id=spec.carry_dataset_id,
        evaluation_spec_hash=evaluation_spec_hash,
        policy=spec.policy,
        plan=spec.plan,
        folds=tuple(folds),
        metrics=metrics,
        passed=not reasons,
        reason_codes=tuple(reasons),
    )


def failed_perpetual_trend_experiment(
    result: PerpetualTrendWalkForwardResult, *, rejected_at: datetime
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的永续趋势评价不能登记为失败实验")
    hypothesis = (
        "ETHUSDT 对称 28 日趋势与 200 日均线共识、30 日波动率定仓的 "
        "USD-M 永续策略，在 10% gross 和现实成本/资金费下具有稳定费用后优势"
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_perpetual_trend", result.evaluation_id),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(f"hypothesis:{hypothesis}", result.evaluation_id),
        rejected_at=require_utc(rejected_at),
        reason_codes=("PERPETUAL_TREND_WALK_FORWARD_FAILED", *result.reason_codes),
    )


def _run_year(
    *,
    carry_dataset: HistoricalCarryDataset,
    indexes: tuple[int, ...],
    year: int,
    policy: PerpetualTrendPolicy,
    starting_equity: Decimal,
) -> PerpetualTrendYearRun:
    days = carry_dataset.days
    settlements_by_day: dict[datetime, list[CarryFundingSettlement]] = defaultdict(list)
    for settlement in carry_dataset.settlements:
        if settlement.funding_time.year == year:
            day_open = settlement.funding_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            settlements_by_day[day_open].append(settlement)

    equity = starting_equity
    peak = equity
    maximum_drawdown = Decimal("0")
    minimum_margin_buffer = Decimal("Infinity")
    quantity = Decimal("0")
    previous_close: Decimal | None = None
    price_pnl = Decimal("0")
    funding_pnl = Decimal("0")
    modeled_cost = Decimal("0")
    rebalance_count = 0
    long_days = 0
    short_days = 0
    cash_days = 0
    liquidated = False
    daily_returns: list[Decimal] = []
    quantity_step = carry_dataset.manifest.instrument.quantity_increment
    closes = tuple(day.contract_close for day in days)

    for index in indexes:
        day = days[index]
        day_start_equity = equity
        if previous_close is not None:
            overnight = quantity * (day.contract_open - previous_close)
            equity += overnight
            price_pnl += overnight

        signal, gross_fraction = _target_signal_and_gross(closes, index, policy)
        raw_quantity = (
            equity * gross_fraction / day.contract_open if equity > 0 else Decimal("0")
        )
        target_abs = floor_to_step(raw_quantity, quantity_step)
        instrument = carry_dataset.manifest.instrument
        if (
            target_abs < instrument.minimum_quantity
            or target_abs * day.contract_open < instrument.minimum_notional
        ):
            target_abs = Decimal("0")
        target_quantity = Decimal(signal) * target_abs
        delta_quantity = target_quantity - quantity
        if (
            delta_quantity != 0
            and abs(delta_quantity) * day.contract_open
            < policy.minimum_rebalance_notional
        ):
            target_quantity = quantity
            delta_quantity = Decimal("0")
        if delta_quantity != 0:
            cost = (
                abs(delta_quantity)
                * day.contract_open
                * policy.one_way_cost_bps
                / Decimal("10000")
            )
            equity -= cost
            modeled_cost += cost
            quantity = target_quantity
            rebalance_count += 1

        peak = max(peak, equity)

        if quantity > 0:
            long_days += 1
            adverse_loss = quantity * max(
                Decimal("0"), day.contract_open - day.mark_low
            )
            worst_mark = day.mark_low
        elif quantity < 0:
            short_days += 1
            adverse_loss = abs(quantity) * max(
                Decimal("0"), day.mark_high - day.contract_open
            )
            worst_mark = day.mark_high
        else:
            cash_days += 1
            adverse_loss = Decimal("0")
            worst_mark = day.mark_close
        maintenance = (
            abs(quantity) * worst_mark * policy.maintenance_margin_fraction
        )
        margin_buffer = (
            equity * policy.margin_budget_fraction - adverse_loss - maintenance
        ) / equity if equity > 0 else Decimal("-1")
        minimum_margin_buffer = min(minimum_margin_buffer, margin_buffer)
        if margin_buffer <= 0:
            liquidated = True
        worst_equity = equity - adverse_loss
        maximum_drawdown = max(
            maximum_drawdown,
            Decimal("1") - worst_equity / peak if peak > 0 else Decimal("1"),
        )

        intraday = quantity * (day.contract_close - day.contract_open)
        equity += intraday
        price_pnl += intraday
        daily_funding = sum(
            (
                -quantity * settlement.mark_price * settlement.funding_rate
                for settlement in settlements_by_day.get(day.open_time, ())
            ),
            Decimal("0"),
        )
        equity += daily_funding
        funding_pnl += daily_funding
        peak = max(peak, equity)
        maximum_drawdown = max(
            maximum_drawdown,
            Decimal("1") - equity / peak if peak > 0 else Decimal("1"),
        )
        daily_returns.append(
            equity / day_start_equity - Decimal("1")
            if day_start_equity > 0
            else Decimal("-1")
        )
        previous_close = day.contract_close

    final_day = days[indexes[-1]]
    closing_cost = (
        abs(quantity)
        * final_day.contract_close
        * policy.one_way_cost_bps
        / Decimal("10000")
    )
    equity -= closing_cost
    modeled_cost += closing_cost
    # Decimal context can round repeated equity additions in a different order than
    # the three attribution ledgers.  Make the decomposed ledger canonical at close.
    equity = starting_equity + price_pnl + funding_pnl - modeled_cost
    if daily_returns:
        before_close = equity + closing_cost
        daily_returns[-1] = (
            (Decimal("1") + daily_returns[-1]) * (equity / before_close)
            - Decimal("1")
            if before_close > 0
            else Decimal("-1")
        )
    maximum_drawdown = max(
        maximum_drawdown,
        Decimal("1") - equity / peak if peak > 0 else Decimal("1"),
    )
    net_pnl = equity - starting_equity
    elapsed_days = Decimal(
        str((final_day.close_time - days[indexes[0]].open_time).total_seconds())
    ) / Decimal("86400")
    metrics = PerpetualTrendRunMetrics(
        starting_equity=starting_equity,
        ending_equity=equity,
        net_pnl=net_pnl,
        price_pnl=price_pnl,
        funding_pnl=funding_pnl,
        modeled_cost=modeled_cost,
        return_fraction=net_pnl / starting_equity,
        simple_annualized_return_fraction=(
            net_pnl / starting_equity * Decimal("365.25") / elapsed_days
        ),
        maximum_drawdown_fraction=maximum_drawdown,
        minimum_margin_buffer_fraction=minimum_margin_buffer,
        rebalance_count=rebalance_count,
        long_days=long_days,
        short_days=short_days,
        cash_days=cash_days,
        liquidated=liquidated,
    )
    weekly_returns = tuple(
        _compound_returns(tuple(daily_returns[offset : offset + 7]))
        for offset in range(0, len(daily_returns) - 6, 7)
    )
    reasons = ("LIQUIDATION_BOUND_BREACHED",) if liquidated else ()
    run_id = stable_id(
        "perpetual_trend_year",
        carry_dataset.manifest.dataset_id,
        policy,
        year,
        metrics,
        weekly_returns,
    )
    return PerpetualTrendYearRun(
        run_id=run_id,
        year=year,
        start=days[indexes[0]].open_time,
        end=final_day.close_time + timedelta(microseconds=1),
        completed=not liquidated,
        reason_codes=reasons,
        weekly_returns=weekly_returns,
        metrics=metrics,
    )


def _target_signal_and_gross(
    closes: tuple[Decimal, ...], index: int, policy: PerpetualTrendPolicy
) -> tuple[int, Decimal]:
    previous_close = closes[index - 1]
    momentum_base = closes[index - 1 - policy.momentum_lookback_days]
    moving_average = _mean(
        tuple(closes[index - policy.regime_moving_average_days : index])
    )
    momentum = previous_close / momentum_base - Decimal("1")
    if momentum > 0 and previous_close > moving_average:
        signal = 1
    elif momentum < 0 and previous_close < moving_average:
        signal = -1
    else:
        return 0, Decimal("0")
    returns = tuple(
        closes[position] / closes[position - 1] - Decimal("1")
        for position in range(index - policy.volatility_lookback_days, index)
    )
    daily_volatility = _sample_standard_deviation(returns)
    annualized_volatility = daily_volatility * Decimal(policy.annualization_days).sqrt()
    if annualized_volatility <= 0:
        return 0, Decimal("0")
    gross = min(
        policy.maximum_gross_exposure_fraction,
        policy.target_annual_volatility_fraction / annualized_volatility,
    )
    return signal, gross


def _is_complete_utc_year(days: tuple, indexes: list[int], year: int) -> bool:
    expected = 366 if _is_leap_year(year) else 365
    return (
        len(indexes) == expected
        and days[indexes[0]].open_time == datetime(year, 1, 1, tzinfo=UTC)
        and days[indexes[-1]].open_time == datetime(year, 12, 31, tzinfo=UTC)
    )


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("均值至少需要一个样本")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _sample_standard_deviation(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        raise ValueError("样本标准差至少需要两个样本")
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(
        len(values) - 1
    )
    return variance.sqrt()


def _mean_lower_bound(values: tuple[Decimal, ...]) -> Decimal:
    return _mean(values) - Decimal("1.96") * _sample_standard_deviation(
        values
    ) / Decimal(len(values)).sqrt()


def _compound_returns(values: tuple[Decimal, ...]) -> Decimal:
    compounded = Decimal("1")
    for value in values:
        compounded *= Decimal("1") + value
    return compounded - Decimal("1")
