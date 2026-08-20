from __future__ import annotations

import json
import platform
from datetime import datetime, timedelta
from decimal import Decimal
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from quant_core.domain import FrozenModel, _require_utc
from quant_core.governance import EvaluationPlan, EvaluationStage, FailedExperiment
from quant_core.ids import content_hash, stable_id, write_json_artifact
from quant_core.research.carry import HistoricalCarryDataset
from quant_core.research.carry_evaluation import (
    CarryBacktestRun,
    CarryPolicy,
    run_carry_backtest,
)
from quant_core.research.dataset import HistoricalDataset, HistoricalFundingDataset


class CarryForwardEvaluationSpec(FrozenModel):
    """Future carry window registered before its market data exists."""

    version: Literal["carry-forward-evaluation-spec-v1"] = (
        "carry-forward-evaluation-spec-v1"
    )
    plan_id: str
    base_manifest_id: str
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_environment: tuple[tuple[str, str], ...] = Field(min_length=2)
    symbol: str = Field(pattern=r"^[A-Z0-9]{5,32}$")
    observation_start: datetime
    observation_end: datetime
    policy: CarryPolicy = Field(default_factory=CarryPolicy)
    starting_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    minimum_calendar_months: Literal[12] = 12
    settlement_grace_days: int = Field(default=7, ge=1, le=31)
    lower_confidence_z: Decimal = Field(default=Decimal("2.201"), gt=0)
    lower_bound_method: Literal["conservative-newey-west-v1"] = (
        "conservative-newey-west-v1"
    )
    newey_west_lag_months: Literal[3] = 3
    minimum_annualized_return_lower_bound: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=1
    )
    minimum_positive_month_fraction: Decimal = Field(
        default=Decimal("0.75"), gt=0, le=1
    )
    minimum_margin_buffer_fraction: Decimal = Field(
        default=Decimal("0.05"), ge=0, le=1
    )
    maximum_one_leg_failure_loss_fraction: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    data_contract: Literal["binance-official-carry-exact-window-v1"] = (
        "binance-official-carry-exact-window-v1"
    )
    report_version: Literal["carry-forward-report-v1"] = "carry-forward-report-v1"

    _utc_observation_start = field_validator("observation_start")(_require_utc)
    _utc_observation_end = field_validator("observation_end")(_require_utc)

    @model_validator(mode="after")
    def window_is_complete_calendar_months(self):
        if tuple(sorted(set(self.evaluator_environment))) != self.evaluator_environment:
            raise ValueError("carry forward 评价环境必须唯一且有序")
        count = len(_calendar_month_windows(self.observation_start, self.observation_end))
        if count < self.minimum_calendar_months:
            raise ValueError("carry forward 正式窗口至少需要十二个完整日历月")
        return self


class CarryForwardMonth(FrozenModel):
    month_id: str
    start: datetime
    end: datetime
    run: CarryBacktestRun

    _utc_start = field_validator("start")(_require_utc)
    _utc_end = field_validator("end")(_require_utc)


class CarryForwardMetrics(FrozenModel):
    average_annualized_return_fraction: Decimal
    annualized_return_lower_bound: Decimal
    positive_month_fraction: Decimal = Field(ge=0, le=1)
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    maximum_one_leg_failure_loss_fraction: Decimal = Field(ge=0)
    continuous_net_pnl: Decimal


class CarryForwardResult(FrozenModel):
    version: Literal["carry-forward-v1"] = "carry-forward-v1"
    result_id: str
    plan_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    policy: CarryPolicy
    observation_start: datetime
    observation_end: datetime
    continuous_run: CarryBacktestRun
    months: tuple[CarryForwardMonth, ...] = Field(min_length=12)
    metrics: CarryForwardMetrics
    passed: bool
    reason_codes: tuple[str, ...]

    _utc_observation_start = field_validator("observation_start")(_require_utc)
    _utc_observation_end = field_validator("observation_end")(_require_utc)


class _CarryForwardEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: CarryForwardResult


class CarryForwardCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: CarryForwardResult) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一 carry forward 结果 ID 的内容不一致")
            return target
        envelope = _CarryForwardEnvelope(
            result_hash=content_hash(result), result=result
        )
        return write_json_artifact(
            root=self._root, target=target, prefix=".carry-forward-", payload=envelope
        )

    def load(self, result_id: str) -> CarryForwardResult:
        raw = json.loads(
            (self._root / f"{result_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(
            raw.get("result")
        ):
            raise ValueError("carry forward 制品内容哈希不匹配")
        envelope = _CarryForwardEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("carry forward 文件名与结果 ID 不一致")
        return envelope.result


def build_carry_forward_evaluation_plan(
    *,
    spec: CarryForwardEvaluationSpec,
    base_manifest_id: str,
    registered_at: datetime,
) -> EvaluationPlan:
    registered_at = _require_utc(registered_at)
    if registered_at >= spec.observation_start:
        raise ValueError("carry forward 计划必须在观察窗口开始前登记")
    if base_manifest_id != spec.base_manifest_id:
        raise ValueError("carry forward 规格与登记时基础 Manifest 不一致")
    return EvaluationPlan(
        plan_id=spec.plan_id,
        registered_at=registered_at,
        base_manifest_id=base_manifest_id,
        primary_metric="annualized_return_lower_bound_vs_cash",
        minimum_sample_size=len(
            _calendar_month_windows(spec.observation_start, spec.observation_end)
        ),
        hard_guardrails=(
            "NO_LIQUIDATION_BOUND_BREACH",
            "ANNUALIZED_RETURN_LOWER_BOUND_POSITIVE_VS_CASH",
            "CONTINUOUS_NET_PNL_POSITIVE_AFTER_COSTS",
            "POSITIVE_MONTH_FRACTION_WITHIN_LIMIT",
            "MAXIMUM_DRAWDOWN_WITHIN_LIMIT",
            "MARGIN_BUFFER_WITHIN_LIMIT",
            "ONE_LEG_FAILURE_LOSS_WITHIN_LIMIT",
        ),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.SHADOW,
        ),
        fixed_regression_suite_version="quant-core-carry-forward-regression-v1",
        candidate_spec_hash=content_hash(spec),
        candidate_spec_snapshot=spec.model_dump(mode="json"),
        blind_query_budget=0,
    )


def validate_carry_forward_evaluation_plan(
    *,
    spec: CarryForwardEvaluationSpec,
    plan: EvaluationPlan,
    evaluated_at: datetime,
    evaluator_code_version: str,
    evaluator_environment: tuple[tuple[str, str], ...],
) -> None:
    evaluated_at = _require_utc(evaluated_at)
    if evaluated_at < spec.observation_end + timedelta(
        days=spec.settlement_grace_days
    ):
        raise ValueError("carry forward 观察窗口或结算宽限期尚未成熟")
    if evaluator_code_version != spec.evaluator_code_version:
        raise ValueError("carry forward 必须使用预登记的精确评价代码版本")
    if evaluator_environment != spec.evaluator_environment:
        raise ValueError("carry forward 必须使用预登记的精确评价依赖环境")
    expected = build_carry_forward_evaluation_plan(
        spec=spec,
        base_manifest_id=spec.base_manifest_id,
        registered_at=plan.registered_at,
    )
    if plan != expected:
        raise ValueError("carry forward EvaluationPlan 与完整预登记合同不一致")


def run_carry_forward_evaluation(
    *,
    spec: CarryForwardEvaluationSpec,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    funding_dataset: HistoricalFundingDataset,
) -> CarryForwardResult:
    _validate_forward_inputs(spec, carry_dataset, spot_dataset, funding_dataset)
    windows = _calendar_month_windows(spec.observation_start, spec.observation_end)
    months = tuple(
        CarryForwardMonth(
            month_id=stable_id("carry_forward_month", spec.plan_id, start, end),
            start=start,
            end=end,
            run=run_carry_backtest(
                carry_dataset=carry_dataset,
                spot_dataset=spot_dataset,
                policy=spec.policy,
                starting_equity=spec.starting_equity,
                start=start,
                end=end,
            ),
        )
        for start, end in windows
    )
    continuous = run_carry_backtest(
        carry_dataset=carry_dataset,
        spot_dataset=spot_dataset,
        policy=spec.policy,
        starting_equity=spec.starting_equity,
        start=spec.observation_start,
        end=spec.observation_end,
    )
    annualized = tuple(
        month.run.metrics.simple_annualized_return_fraction for month in months
    )
    metrics = CarryForwardMetrics(
        average_annualized_return_fraction=sum(annualized, Decimal("0"))
        / len(annualized),
        annualized_return_lower_bound=_conservative_newey_west_lower_bound(
            annualized,
            z=spec.lower_confidence_z,
            lag=spec.newey_west_lag_months,
        ),
        positive_month_fraction=Decimal(sum(item > 0 for item in annualized))
        / len(annualized),
        maximum_drawdown_fraction=max(
            continuous.metrics.maximum_drawdown_fraction,
            *(month.run.metrics.maximum_drawdown_fraction for month in months),
        ),
        minimum_margin_buffer_fraction=min(
            continuous.metrics.minimum_margin_buffer_fraction,
            *(month.run.metrics.minimum_margin_buffer_fraction for month in months),
        ),
        maximum_one_leg_failure_loss_fraction=max(
            continuous.metrics.maximum_one_leg_failure_loss_fraction,
            *(
                month.run.metrics.maximum_one_leg_failure_loss_fraction
                for month in months
            ),
        ),
        continuous_net_pnl=continuous.metrics.net_pnl,
    )
    reasons = _forward_gate_reasons(spec, continuous, months, metrics)
    spec_hash = content_hash(spec)
    result_id = stable_id(
        "carry_forward",
        spec.plan_id,
        spec_hash,
        carry_dataset.manifest.dataset_id,
        continuous.run_id,
        tuple(month.run.run_id for month in months),
        metrics,
    )
    return CarryForwardResult(
        result_id=result_id,
        plan_id=spec.plan_id,
        evaluation_spec_hash=spec_hash,
        dataset_id=carry_dataset.manifest.dataset_id,
        spot_dataset_id=carry_dataset.manifest.spot_dataset_id,
        funding_dataset_id=carry_dataset.manifest.funding_dataset_id,
        policy=spec.policy,
        observation_start=spec.observation_start,
        observation_end=spec.observation_end,
        continuous_run=continuous,
        months=months,
        metrics=metrics,
        passed=not reasons,
        reason_codes=reasons,
    )


def failed_carry_forward_experiment(
    result: CarryForwardResult, *, rejected_at: datetime
) -> FailedExperiment:
    if result.passed:
        raise ValueError("通过的 carry forward 不能登记为失败实验")
    hypothesis = (
        f"前向程序策略 {result.policy.family}/{result.policy.version} 在 "
        f"{result.observation_start.isoformat()} 至 {result.observation_end.isoformat()} "
        "以月度同数量现货多头与永续空头满足现金基线、收益下界、保证金、"
        "回撤和单腿失败压力门槛"
    )
    return FailedExperiment(
        experiment_id=stable_id("failed_carry_forward", result.result_id),
        hypothesis_fingerprint=content_hash(
            {"hypothesis": hypothesis.strip().lower()}
        ),
        evidence_ids=(f"hypothesis:{hypothesis}", result.result_id),
        rejected_at=_require_utc(rejected_at),
        reason_codes=("CARRY_FORWARD_FAILED", *result.reason_codes),
    )


def _forward_gate_reasons(
    spec: CarryForwardEvaluationSpec,
    continuous: CarryBacktestRun,
    months: tuple[CarryForwardMonth, ...],
    metrics: CarryForwardMetrics,
) -> tuple[str, ...]:
    reasons = []
    if not continuous.completed or any(not month.run.completed for month in months):
        reasons.append("LIQUIDATION_BOUND_BREACHED")
    if (
        metrics.annualized_return_lower_bound
        <= spec.minimum_annualized_return_lower_bound
    ):
        reasons.append("ANNUALIZED_RETURN_LOWER_BOUND_NOT_POSITIVE")
    if metrics.continuous_net_pnl <= 0:
        reasons.append("CONTINUOUS_NET_PNL_NOT_POSITIVE")
    if metrics.positive_month_fraction < spec.minimum_positive_month_fraction:
        reasons.append("POSITIVE_MONTH_FRACTION_BELOW_GATE")
    if metrics.maximum_drawdown_fraction > spec.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if metrics.minimum_margin_buffer_fraction < spec.minimum_margin_buffer_fraction:
        reasons.append("MARGIN_BUFFER_BELOW_GATE")
    if (
        metrics.maximum_one_leg_failure_loss_fraction
        > spec.maximum_one_leg_failure_loss_fraction
    ):
        reasons.append("ONE_LEG_FAILURE_LOSS_EXCEEDED")
    return tuple(reasons)


def _validate_forward_inputs(
    spec: CarryForwardEvaluationSpec,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    funding_dataset: HistoricalFundingDataset,
) -> None:
    carry = carry_dataset.manifest
    spot = spot_dataset.manifest
    funding = funding_dataset.manifest
    carry_funding = tuple(
        (
            item.symbol,
            item.funding_time,
            item.available_at,
            item.funding_interval_hours,
            item.funding_rate,
        )
        for item in carry_dataset.settlements
    )
    official_funding = tuple(
        (
            item.symbol,
            item.funding_time,
            item.available_at,
            item.funding_interval_hours,
            item.funding_rate,
        )
        for item in funding_dataset.observations
    )
    if (
        carry.spot_dataset_id != spot.dataset_id
        or carry.funding_dataset_id != funding.dataset_id
        or tuple(item.open_time for item in carry_dataset.days)
        != tuple(item.open_time for item in spot_dataset.bars)
        or carry_funding != official_funding
        or carry.symbol != spec.symbol
        or spot.symbol != spec.symbol
        or funding.symbol != spec.symbol
        or carry.requested_start != spec.observation_start
        or carry.requested_end != spec.observation_end
        or spot.requested_start != spec.observation_start
        or spot.requested_end != spec.observation_end
        or funding.requested_start != spec.observation_start
        or funding.requested_end != spec.observation_end
        or carry.collected_at < spec.observation_end
        or spot.collected_at < spec.observation_end
        or funding.collected_at < spec.observation_end
        or spot.interval != "1d"
        or spot.source != "binance-rest-historical"
    ):
        raise ValueError("carry forward 数据源、作用域或精确窗口与预登记不一致")


def _calendar_month_windows(
    start: datetime, end: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    if start >= end or not _is_month_boundary(start) or not _is_month_boundary(end):
        raise ValueError("carry forward 窗口必须是非空完整 UTC 日历月")
    windows = []
    cursor = start
    while cursor < end:
        following = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
        if following > end:
            raise ValueError("carry forward 窗口终点没有按日历月对齐")
        windows.append((cursor, following))
        cursor = following
    return tuple(windows)


def _is_month_boundary(value: datetime) -> bool:
    return (
        value.day == 1
        and value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _conservative_newey_west_lower_bound(
    values: tuple[Decimal, ...], *, z: Decimal, lag: int
) -> Decimal:
    if len(values) < 2:
        raise ValueError("carry forward 保守下界至少需要两个独立月份")
    if lag < 1 or lag >= len(values):
        raise ValueError("carry forward Newey-West lag 必须小于月份数")
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
        weight = Decimal(1) - Decimal(offset) / Decimal(lag + 1)
        long_run_variance += Decimal(2) * weight * covariance
    # 负自相关不能让证据门槛比独立月份假设更宽松。
    conservative_variance = max(gamma_zero, long_run_variance, Decimal("0"))
    return mean - z * (conservative_variance / count).sqrt()


def current_carry_evaluator_environment() -> tuple[tuple[str, str], ...]:
    """Small deterministic environment contract for the pure carry evaluator."""

    return (
        ("pydantic", distribution_version("pydantic")),
        ("python", platform.python_version()),
    )
