from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from quant_core.domain import FrozenModel, _require_utc, floor_to_step
from quant_core.governance import EvaluationPlan, EvaluationStage, FailedExperiment
from quant_core.ids import content_hash, stable_id, write_json_artifact
from quant_core.research.carry import (
    CarryFundingSettlement,
    HistoricalCarryDataset,
)
from quant_core.research.dataset import HistoricalDataset


class CarryPolicy(FrozenModel):
    strategy_id: Literal["btc-spot-perp-carry", "spot-perp-carry"] = (
        "spot-perp-carry"
    )
    version: Literal[
        "btc-spot-perp-monthly-50pct-v1", "spot-perp-monthly-50pct-v1"
    ] = (
        "spot-perp-monthly-50pct-v1"
    )
    family: Literal["delta-neutral-funding-carry"] = "delta-neutral-funding-carry"
    leg_equity_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, le=Decimal("0.5"))
    spot_cost_bps: Decimal = Field(default=Decimal("12.5"), ge=0)
    futures_cost_bps: Decimal = Field(default=Decimal("7.5"), ge=0)
    maintenance_margin_fraction: Decimal = Field(
        default=Decimal("0.10"), gt=0, lt=1
    )
    one_leg_failure_move_bps: Decimal = Field(default=Decimal("100"), gt=0)


class CarryRunMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal
    net_pnl: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    return_fraction: Decimal
    simple_annualized_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    maximum_one_leg_failure_loss_fraction: Decimal = Field(ge=0)
    rebalance_count: int = Field(ge=1)
    liquidated: bool

    @model_validator(mode="after")
    def pnl_reconciles(self):
        if self.ending_equity - self.starting_equity != self.net_pnl:
            raise ValueError("carry 权益与净损益不一致")
        if self.funding_pnl + self.basis_pnl - self.modeled_cost != self.net_pnl:
            raise ValueError("carry 资金费、基差和成本无法核对")
        return self


class CarryBacktestRun(FrozenModel):
    version: Literal["carry-backtest-v1"] = "carry-backtest-v1"
    run_id: str
    dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    policy: CarryPolicy
    start: datetime
    end: datetime
    completed: bool
    reason_codes: tuple[str, ...]
    assumptions: tuple[str, ...]
    metrics: CarryRunMetrics

    _utc_start = field_validator("start")(_require_utc)
    _utc_end = field_validator("end")(_require_utc)


class CarryWalkForwardPlan(FrozenModel):
    plan_id: str
    fold_count: int = Field(default=5, ge=3)
    blind_days: int = Field(default=365, ge=30)
    starting_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    minimum_annualized_return_lower_bound: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=1
    )
    minimum_positive_fold_fraction: Decimal = Field(
        default=Decimal("0.75"), gt=0, le=1
    )
    minimum_margin_buffer_fraction: Decimal = Field(
        default=Decimal("0.05"), ge=0, le=1
    )
    maximum_one_leg_failure_loss_fraction: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )


class CarryWalkForwardFold(FrozenModel):
    fold_id: str
    start: datetime
    end: datetime
    run: CarryBacktestRun

    _utc_start = field_validator("start")(_require_utc)
    _utc_end = field_validator("end")(_require_utc)


class CarryWalkForwardMetrics(FrozenModel):
    average_annualized_return_fraction: Decimal
    annualized_return_lower_bound: Decimal
    positive_fold_fraction: Decimal = Field(ge=0, le=1)
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    maximum_one_leg_failure_loss_fraction: Decimal = Field(ge=0)
    aggregate_net_pnl: Decimal


class CarryWalkForwardResult(FrozenModel):
    version: Literal["carry-walk-forward-v1"] = "carry-walk-forward-v1"
    evaluation_id: str
    dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    evaluation_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    policy: CarryPolicy
    plan: CarryWalkForwardPlan
    blind_start: datetime
    blind_end: datetime
    folds: tuple[CarryWalkForwardFold, ...]
    metrics: CarryWalkForwardMetrics
    passed: bool
    reason_codes: tuple[str, ...]

    _utc_blind_start = field_validator("blind_start")(_require_utc)
    _utc_blind_end = field_validator("blind_end")(_require_utc)


class CarryBlindResult(FrozenModel):
    version: Literal["carry-blind-v1"] = "carry-blind-v1"
    result_id: str
    query_id: str
    source_evaluation_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    policy: CarryPolicy
    plan: CarryWalkForwardPlan
    run: CarryBacktestRun
    passed: bool
    reason_codes: tuple[str, ...]


class CarryEvaluationSpec(FrozenModel):
    version: Literal["carry-evaluation-spec-v1"] = "carry-evaluation-spec-v1"
    carry_dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    policy: CarryPolicy
    plan: CarryWalkForwardPlan

    @classmethod
    def freeze(
        cls,
        *,
        carry_dataset: HistoricalCarryDataset,
        spot_dataset: HistoricalDataset,
        policy: CarryPolicy,
        plan: CarryWalkForwardPlan,
    ) -> CarryEvaluationSpec:
        _validate_external_carry_inputs(carry_dataset, spot_dataset)
        return cls(
            carry_dataset_id=carry_dataset.manifest.dataset_id,
            spot_dataset_id=carry_dataset.manifest.spot_dataset_id,
            funding_dataset_id=carry_dataset.manifest.funding_dataset_id,
            policy=policy,
            plan=plan,
        )


class CarryEvaluationEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: CarryWalkForwardResult


class CarryBlindEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: CarryBlindResult


class CarryEvaluationCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: CarryWalkForwardResult) -> Path:
        target = self._root / f"{result.evaluation_id}.json"
        if target.exists():
            if self.load(result.evaluation_id) != result:
                raise ValueError("同一 carry 评价 ID 的内容不一致")
            return target
        envelope = CarryEvaluationEnvelope(
            result_hash=content_hash(result), result=result
        )
        return write_json_artifact(
            root=self._root, target=target, prefix=".carry-evaluation-", payload=envelope
        )

    def load(self, evaluation_id: str) -> CarryWalkForwardResult:
        raw = json.loads(
            (self._root / f"{evaluation_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(
            raw.get("result")
        ):
            raise ValueError("carry 评价制品内容哈希不匹配")
        envelope = CarryEvaluationEnvelope.model_validate(raw)
        if envelope.result.evaluation_id != evaluation_id:
            raise ValueError("carry 评价文件名与内容 ID 不一致")
        return envelope.result


class CarryBlindCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: CarryBlindResult) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一 carry 盲测 ID 的内容不一致")
            return target
        envelope = CarryBlindEnvelope(result_hash=content_hash(result), result=result)
        return write_json_artifact(
            root=self._root, target=target, prefix=".carry-blind-", payload=envelope
        )

    def load(self, result_id: str) -> CarryBlindResult:
        raw = json.loads(
            (self._root / f"{result_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(
            raw.get("result")
        ):
            raise ValueError("carry 盲测制品内容哈希不匹配")
        envelope = CarryBlindEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("carry 盲测文件名与内容 ID 不一致")
        return envelope.result


def build_carry_evaluation_plan(
    *, spec: CarryEvaluationSpec, base_manifest_id: str, registered_at: datetime
) -> EvaluationPlan:
    return EvaluationPlan(
        plan_id=spec.plan.plan_id,
        registered_at=_require_utc(registered_at),
        base_manifest_id=base_manifest_id,
        primary_metric="annualized_return_lower_bound",
        minimum_sample_size=spec.plan.fold_count,
        hard_guardrails=(
            "ANNUALIZED_RETURN_LOWER_BOUND_POSITIVE",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
            "MARGIN_BUFFER_WITHIN_LIMIT",
            "ONE_LEG_FAILURE_LOSS_WITHIN_LIMIT",
        ),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
            EvaluationStage.BLIND,
        ),
        fixed_regression_suite_version="quant-core-carry-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=1,
    )


def validate_carry_evaluation_plan(
    *,
    spec: CarryEvaluationSpec,
    plan: EvaluationPlan,
    champion_manifest_id: str,
    evaluated_at: datetime,
) -> None:
    if plan.plan_id != spec.plan.plan_id:
        raise ValueError("carry 评价与预登记计划 ID 不一致")
    if plan.registered_at > _require_utc(evaluated_at):
        raise ValueError("carry 评价不能早于计划预登记时间")
    if plan.base_manifest_id != champion_manifest_id:
        raise ValueError("carry 计划不属于当前 Champion")
    if plan.candidate_spec_hash != content_hash(spec):
        raise ValueError("carry 数据、策略、成本、压力或门禁与预登记规格不一致")
    if plan.minimum_sample_size != spec.plan.fold_count:
        raise ValueError("carry 分折样本数与预登记计划不一致")
    required = {
        EvaluationStage.STATIC,
        EvaluationStage.FIXED_REGRESSION,
        EvaluationStage.WALK_FORWARD,
        EvaluationStage.BLIND,
    }
    if not required.issubset(plan.required_stages) or plan.blind_query_budget != 1:
        raise ValueError("carry 计划缺少完整评价阶段或唯一盲测预算")


def failed_carry_walk_forward_experiment(
    result: CarryWalkForwardResult, *, rejected_at: datetime
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的 carry walk-forward 不能登记为失败实验")
    hypothesis = (
        f"历史程序策略 {result.policy.family}/{result.policy.version} 在 "
        f"{result.dataset_id} 以月度同数量现货多头与永续空头满足费用后收益、"
        "保证金、回撤和单腿失败压力门槛"
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_carry_walk_forward", result.evaluation_id),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(f"hypothesis:{hypothesis}", result.evaluation_id),
        rejected_at=_require_utc(rejected_at),
        reason_codes=("CARRY_WALK_FORWARD_FAILED", *result.reason_codes),
    )


def failed_carry_blind_experiment(
    result: CarryBlindResult, *, rejected_at: datetime
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的 carry 盲测不能登记为失败实验")
    hypothesis = (
        f"历史程序策略 {result.policy.family}/{result.policy.version} 在 "
        f"{result.dataset_id} 的一次性尾窗满足费用后收益、保证金、回撤和单腿失败门槛"
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_carry_blind", result.result_id),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(
            f"hypothesis:{hypothesis}",
            result.source_evaluation_id,
            result.result_id,
        ),
        rejected_at=_require_utc(rejected_at),
        reason_codes=("CARRY_BLIND_FAILED", *result.reason_codes),
    )


def run_carry_backtest(
    *,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    policy: CarryPolicy,
    starting_equity: Decimal,
    start: datetime,
    end: datetime,
) -> CarryBacktestRun:
    """Replay one fixed spot/perpetual pair; no signal fitting or AI calls."""

    start = _require_utc(start)
    end = _require_utc(end)
    if start >= end:
        raise ValueError("carry 回放起点必须早于终点")
    _validate_external_carry_inputs(carry_dataset, spot_dataset)
    spot_by_open = {item.open_time: item for item in spot_dataset.bars}
    days = tuple(item for item in carry_dataset.days if start <= item.open_time < end)
    if not days or days[0].open_time != start:
        raise ValueError("carry 回放窗口必须从冻结日线开盘开始")
    if days[-1].close_time >= end:
        raise ValueError("carry 回放终点必须晚于最后一根完整日线")
    settlements_by_day: dict[datetime, list[CarryFundingSettlement]] = {}
    for settlement in carry_dataset.settlements:
        if start <= settlement.funding_time < end:
            day_open = settlement.funding_time.replace(hour=0, minute=0, second=0, microsecond=0)
            settlements_by_day.setdefault(day_open, []).append(settlement)

    equity = starting_equity
    peak = equity
    maximum_drawdown = Decimal("0")
    quantity = Decimal("0")
    previous_spot_close: Decimal | None = None
    previous_contract_close: Decimal | None = None
    futures_margin = Decimal("0")
    funding_pnl = Decimal("0")
    basis_pnl = Decimal("0")
    modeled_cost = Decimal("0")
    minimum_margin_buffer = Decimal("Infinity")
    maximum_failure_loss = Decimal("0")
    rebalance_count = 0
    liquidated = False
    quantity_step = max(
        spot_dataset.manifest.instrument.quantity_increment,
        carry_dataset.manifest.instrument.quantity_increment,
    )
    previous_month: tuple[int, int] | None = None

    for day in days:
        spot = spot_by_open[day.open_time]
        if previous_spot_close is not None and previous_contract_close is not None:
            overnight = quantity * (
                (spot.open - previous_spot_close)
                - (day.contract_open - previous_contract_close)
            )
            equity += overnight
            basis_pnl += overnight
            futures_margin -= quantity * (day.contract_open - previous_contract_close)

        month = (day.open_time.year, day.open_time.month)
        if month != previous_month:
            target_notional = equity * policy.leg_equity_fraction
            raw_quantity = target_notional / max(spot.open, day.contract_open)
            target_quantity = floor_to_step(raw_quantity, quantity_step)
            if (
                target_quantity < spot_dataset.manifest.instrument.minimum_quantity
                or target_quantity < carry_dataset.manifest.instrument.minimum_quantity
                or target_quantity * spot.open
                < spot_dataset.manifest.instrument.minimum_notional
                or target_quantity * day.contract_open
                < carry_dataset.manifest.instrument.minimum_notional
            ):
                raise ValueError("carry 目标双腿低于冻结交易规则")
            delta = abs(target_quantity - quantity)
            cost = delta * (
                spot.open * policy.spot_cost_bps
                + day.contract_open * policy.futures_cost_bps
            ) / Decimal("10000")
            equity -= cost
            modeled_cost += cost
            quantity = target_quantity
            futures_margin = equity - quantity * spot.open
            rebalance_count += 1
            previous_month = month
            failure_loss = (
                quantity
                * max(spot.open, day.contract_open)
                * policy.one_leg_failure_move_bps
                / Decimal("10000")
                / equity
            )
            maximum_failure_loss = max(maximum_failure_loss, failure_loss)

        worst_short_loss = quantity * max(
            Decimal("0"), day.mark_high - day.contract_open
        )
        maintenance = (
            quantity * day.mark_high * policy.maintenance_margin_fraction
        )
        margin_buffer = (futures_margin - worst_short_loss - maintenance) / equity
        minimum_margin_buffer = min(minimum_margin_buffer, margin_buffer)
        if margin_buffer <= 0:
            liquidated = True

        intraday = quantity * (
            (spot.close - spot.open) - (day.contract_close - day.contract_open)
        )
        equity += intraday
        basis_pnl += intraday
        futures_margin -= quantity * (day.contract_close - day.contract_open)
        daily_funding = sum(
            (
                quantity * settlement.mark_price * settlement.funding_rate
                for settlement in settlements_by_day.get(day.open_time, ())
            ),
            Decimal("0"),
        )
        equity += daily_funding
        futures_margin += daily_funding
        funding_pnl += daily_funding
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)
        previous_spot_close = spot.close
        previous_contract_close = day.contract_close

    assert previous_spot_close is not None and previous_contract_close is not None
    closing_cost = quantity * (
        previous_spot_close * policy.spot_cost_bps
        + previous_contract_close * policy.futures_cost_bps
    ) / Decimal("10000")
    equity -= closing_cost
    modeled_cost += closing_cost
    maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)
    net_pnl = equity - starting_equity
    elapsed_seconds = (days[-1].close_time - days[0].open_time).total_seconds()
    elapsed_days = Decimal(str(elapsed_seconds)) / Decimal("86400")
    annualized = (net_pnl / starting_equity) * Decimal("365.25") / elapsed_days
    reason_codes = ("LIQUIDATION_BOUND_BREACHED",) if liquidated else ()
    assumptions = (
        "SAME_BASE_QUANTITY_SPOT_LONG_PERPETUAL_SHORT",
        "MONTHLY_FIRST_OPEN_REBALANCE",
        "FUNDING_MARK_FROM_8H_PRE_SETTLEMENT_CLOSE",
        "CURRENT_RULE_SNAPSHOT_WITH_CONSERVATIVE_MAINTENANCE_MARGIN",
        "ONE_LEG_FAILURE_FIXED_PRICE_SHOCK",
        "NO_CODEX_REPLAY",
    )
    metrics = CarryRunMetrics(
        starting_equity=starting_equity,
        ending_equity=equity,
        net_pnl=net_pnl,
        funding_pnl=funding_pnl,
        basis_pnl=basis_pnl,
        modeled_cost=modeled_cost,
        return_fraction=net_pnl / starting_equity,
        simple_annualized_return_fraction=annualized,
        maximum_drawdown_fraction=maximum_drawdown,
        minimum_margin_buffer_fraction=minimum_margin_buffer,
        maximum_one_leg_failure_loss_fraction=maximum_failure_loss,
        rebalance_count=rebalance_count,
        liquidated=liquidated,
    )
    run_id = stable_id(
        "carry_backtest",
        carry_dataset.manifest.dataset_id,
        policy,
        starting_equity,
        start,
        end,
        metrics,
    )
    return CarryBacktestRun(
        run_id=run_id,
        dataset_id=carry_dataset.manifest.dataset_id,
        spot_dataset_id=carry_dataset.manifest.spot_dataset_id,
        funding_dataset_id=carry_dataset.manifest.funding_dataset_id,
        policy=policy,
        start=start,
        end=end,
        completed=not liquidated,
        reason_codes=reason_codes,
        assumptions=assumptions,
        metrics=metrics,
    )


def run_carry_walk_forward(
    *,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    policy: CarryPolicy,
    plan: CarryWalkForwardPlan,
    evaluation_spec_hash: str | None = None,
) -> CarryWalkForwardResult:
    _validate_external_carry_inputs(carry_dataset, spot_dataset)
    day_count = len(carry_dataset.days)
    development_count = day_count - plan.blind_days
    if development_count < plan.fold_count * 30:
        raise ValueError("carry 开发区不足以形成预登记分折")
    base, remainder = divmod(development_count, plan.fold_count)
    folds: list[CarryWalkForwardFold] = []
    cursor = 0
    for index in range(plan.fold_count):
        size = base + int(index < remainder)
        selected = carry_dataset.days[cursor : cursor + size]
        start = selected[0].open_time
        end = selected[-1].close_time + timedelta(microseconds=1)
        run = run_carry_backtest(
            carry_dataset=carry_dataset,
            spot_dataset=spot_dataset,
            policy=policy,
            starting_equity=plan.starting_equity,
            start=start,
            end=end,
        )
        folds.append(
            CarryWalkForwardFold(
                fold_id=stable_id("carry_fold", plan.plan_id, index, start, end),
                start=start,
                end=end,
                run=run,
            )
        )
        cursor += size
    blind_days = carry_dataset.days[development_count:]
    annualized = tuple(
        item.run.metrics.simple_annualized_return_fraction for item in folds
    )
    average = sum(annualized, Decimal("0")) / len(annualized)
    lower_bound = _decimal_mean_lower_bound(annualized)
    metrics = CarryWalkForwardMetrics(
        average_annualized_return_fraction=average,
        annualized_return_lower_bound=lower_bound,
        positive_fold_fraction=Decimal(sum(item > 0 for item in annualized))
        / len(annualized),
        maximum_drawdown_fraction=max(
            item.run.metrics.maximum_drawdown_fraction for item in folds
        ),
        minimum_margin_buffer_fraction=min(
            item.run.metrics.minimum_margin_buffer_fraction for item in folds
        ),
        maximum_one_leg_failure_loss_fraction=max(
            item.run.metrics.maximum_one_leg_failure_loss_fraction for item in folds
        ),
        aggregate_net_pnl=sum(
            (item.run.metrics.net_pnl for item in folds), Decimal("0")
        ),
    )
    reasons = []
    if any(not item.run.completed for item in folds):
        reasons.append("LIQUIDATION_BOUND_BREACHED")
    if metrics.annualized_return_lower_bound <= plan.minimum_annualized_return_lower_bound:
        reasons.append("ANNUALIZED_RETURN_LOWER_BOUND_NOT_POSITIVE")
    if metrics.maximum_drawdown_fraction > plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if metrics.positive_fold_fraction < plan.minimum_positive_fold_fraction:
        reasons.append("POSITIVE_FOLD_FRACTION_BELOW_GATE")
    if metrics.minimum_margin_buffer_fraction < plan.minimum_margin_buffer_fraction:
        reasons.append("MARGIN_BUFFER_BELOW_GATE")
    if (
        metrics.maximum_one_leg_failure_loss_fraction
        > plan.maximum_one_leg_failure_loss_fraction
    ):
        reasons.append("ONE_LEG_FAILURE_LOSS_EXCEEDED")
    evaluation_id = stable_id(
        "carry_walk_forward",
        carry_dataset.manifest.dataset_id,
        policy,
        plan,
        evaluation_spec_hash,
        tuple(fold.run.run_id for fold in folds),
        metrics,
    )
    return CarryWalkForwardResult(
        evaluation_id=evaluation_id,
        dataset_id=carry_dataset.manifest.dataset_id,
        spot_dataset_id=carry_dataset.manifest.spot_dataset_id,
        funding_dataset_id=carry_dataset.manifest.funding_dataset_id,
        evaluation_spec_hash=evaluation_spec_hash,
        policy=policy,
        plan=plan,
        blind_start=blind_days[0].open_time,
        blind_end=blind_days[-1].close_time + timedelta(microseconds=1),
        folds=tuple(folds),
        metrics=metrics,
        passed=not reasons,
        reason_codes=tuple(reasons),
    )


def run_carry_blind_evaluation(
    *,
    source: CarryWalkForwardResult,
    query_id: str,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
) -> CarryBlindResult:
    if not source.passed or source.evaluation_spec_hash is None:
        raise ValueError("carry 源 walk-forward 未通过或没有预登记规格")
    if (
        source.dataset_id != carry_dataset.manifest.dataset_id
        or source.spot_dataset_id != spot_dataset.manifest.dataset_id
        or source.funding_dataset_id != carry_dataset.manifest.funding_dataset_id
    ):
        raise ValueError("carry 盲测数据与源评价身份不一致")
    run = run_carry_backtest(
        carry_dataset=carry_dataset,
        spot_dataset=spot_dataset,
        policy=source.policy,
        starting_equity=source.plan.starting_equity,
        start=source.blind_start,
        end=source.blind_end,
    )
    reasons = []
    if not run.completed:
        reasons.append("LIQUIDATION_BOUND_BREACHED")
    if (
        run.metrics.simple_annualized_return_fraction
        <= source.plan.minimum_annualized_return_lower_bound
    ):
        reasons.append("ANNUALIZED_RETURN_NOT_POSITIVE")
    if run.metrics.maximum_drawdown_fraction > source.plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if (
        run.metrics.minimum_margin_buffer_fraction
        < source.plan.minimum_margin_buffer_fraction
    ):
        reasons.append("MARGIN_BUFFER_BELOW_GATE")
    if (
        run.metrics.maximum_one_leg_failure_loss_fraction
        > source.plan.maximum_one_leg_failure_loss_fraction
    ):
        reasons.append("ONE_LEG_FAILURE_LOSS_EXCEEDED")
    result_id = stable_id(
        "carry_blind",
        query_id,
        source.evaluation_id,
        source.evaluation_spec_hash,
        run.run_id,
        tuple(reasons),
    )
    return CarryBlindResult(
        result_id=result_id,
        query_id=query_id,
        source_evaluation_id=source.evaluation_id,
        evaluation_spec_hash=source.evaluation_spec_hash,
        dataset_id=source.dataset_id,
        spot_dataset_id=source.spot_dataset_id,
        funding_dataset_id=source.funding_dataset_id,
        policy=source.policy,
        plan=source.plan,
        run=run,
        passed=not reasons,
        reason_codes=tuple(reasons),
    )


def _validate_external_carry_inputs(
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
) -> None:
    if (
        carry_dataset.manifest.spot_dataset_id != spot_dataset.manifest.dataset_id
        or carry_dataset.manifest.symbol != spot_dataset.manifest.symbol
        or carry_dataset.manifest.requested_start != spot_dataset.manifest.requested_start
        or carry_dataset.manifest.requested_end != spot_dataset.manifest.requested_end
        or tuple(item.open_time for item in carry_dataset.days)
        != tuple(item.open_time for item in spot_dataset.bars)
    ):
        raise ValueError("carry 数据束与现货制品身份或时间轴不一致")


def _decimal_mean_lower_bound(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        raise ValueError("carry 保守下界至少需要两个独立分折")
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    variance = sum(((item - mean) ** 2 for item in values), Decimal("0")) / (
        count - 1
    )
    standard_error = (variance / count).sqrt()
    return mean - Decimal("1.96") * standard_error
