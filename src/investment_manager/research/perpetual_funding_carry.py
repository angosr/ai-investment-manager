from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import ClosedMarketBar
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.carry import (
    CarryFundingSettlement,
    CarryMarketBar,
    HistoricalCarryDataset,
)
from investment_manager.research.dataset import HistoricalDataset

_BPS = Decimal("10000")
_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class EvaluationGate:
    minimum_entered_windows: int
    minimum_positive_window_fraction: Decimal
    minimum_positive_subperiods: int
    subperiod_count: int
    minimum_net_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal
    liquidation_count: int


@dataclass(frozen=True, slots=True)
class PerpetualFundingCarryPlan:
    plan_id: str
    plan_hash: str
    carry_dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    source_start: datetime
    source_end: datetime
    partitions: tuple[tuple[str, datetime, datetime], ...]
    starting_equity: Decimal
    formation_days: int
    holding_days: int
    leg_notional_fraction: Decimal
    quantity_step: Decimal
    minimum_notional: Decimal
    spot_fee_bps: Decimal
    perpetual_fee_bps: Decimal
    friction_bps: Decimal
    total_round_trip_bps: Decimal
    maintenance_margin_fraction: Decimal
    validation_gate: EvaluationGate
    blind_gate: EvaluationGate


class FundingCarryWindowOutcome(FrozenModel):
    partition: str
    entry_time: datetime
    exit_time: datetime
    status: Literal["SKIPPED_COST", "ENTERED", "LIQUIDATED", "INCOMPLETE"]
    reason: str | None = None
    equity_before: Decimal = Field(gt=0)
    equity_after: Decimal = Field(ge=0)
    trailing_funding_fraction: Decimal
    quantity: Decimal = Field(ge=0)
    spot_entry: Decimal | None = Field(default=None, gt=0)
    perpetual_entry: Decimal | None = Field(default=None, gt=0)
    spot_exit: Decimal | None = Field(default=None, gt=0)
    perpetual_exit: Decimal | None = Field(default=None, gt=0)
    funding_pnl: Decimal
    basis_and_price_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    net_pnl: Decimal
    return_fraction: Decimal
    minimum_margin_buffer_fraction: Decimal | None = None
    maximum_drawdown_fraction: Decimal = Field(default=Decimal("0"), ge=0)

    _utc_entry = field_validator("entry_time")(require_utc)
    _utc_exit = field_validator("exit_time")(require_utc)


class FundingCarrySubperiodOutcome(FrozenModel):
    start: datetime
    end: datetime
    entered_window_count: int = Field(ge=0)
    net_return_fraction: Decimal

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)


class FundingCarryPartitionOutcome(FrozenModel):
    partition: str
    start: datetime
    end: datetime
    eligible_window_count: int = Field(ge=0)
    entered_window_count: int = Field(ge=0)
    positive_entered_window_fraction: Decimal | None = None
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal = Field(ge=0)
    net_return_fraction: Decimal
    funding_pnl: Decimal
    basis_and_price_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal | None = None
    liquidation_count: int = Field(ge=0)
    subperiods: tuple[FundingCarrySubperiodOutcome, ...]
    gate_reasons: tuple[str, ...]
    windows: tuple[FundingCarryWindowOutcome, ...]

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)


class PerpetualFundingCarryResult(FrozenModel):
    schema_version: Literal["perpetual-funding-carry-result-v1"] = (
        "perpetual-funding-carry-result-v1"
    )
    result_id: str
    plan_id: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    carry_dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    evaluated_at: datetime
    blind_revealed: bool
    status: Literal[
        "REJECTED_VALIDATION",
        "VALIDATION_PASSED_BLIND_RESERVED",
        "REJECTED_BLIND",
        "PASSED_RETROSPECTIVE",
    ]
    reason_codes: tuple[str, ...]
    development: FundingCarryPartitionOutcome
    validation: FundingCarryPartitionOutcome
    blind: FundingCarryPartitionOutcome | None = None
    capital_authorization: Literal["NONE"] = "NONE"
    limitations: tuple[str, ...]

    _utc_evaluated = field_validator("evaluated_at")(require_utc)


def load_perpetual_funding_carry_plan(path: Path) -> PerpetualFundingCarryPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("永续 funding carry 计划必须是对象")
    if raw.get("schema_version") != "perpetual-funding-carry-candidate-plan-v1":
        raise ValueError("永续 funding carry 计划 Schema 不一致")
    if raw.get("plan_id") != "btc-perpetual-funding-carry-v1":
        raise ValueError("永续 funding carry 计划身份不一致")
    data = raw["data"]
    rule = raw["rule"]
    cost = raw["cost"]
    risk = raw["risk"]
    evaluation = raw["evaluation"]
    required = {
        rule.get("windows"): "non_overlapping_within_each_partition",
        rule.get("signal"): "sum_of_settled_funding_rates_available_before_entry",
        rule.get("entry_gate"): (
            "trailing_funding_sum_strictly_above_complete_round_trip_cost_fraction"
        ),
        rule.get("parameter_search"): "prohibited",
        evaluation.get("permission"): "REJECTION_OR_FORWARD_RESEARCH_ONLY",
    }
    for actual, expected in required.items():
        if actual != expected:
            raise ValueError(f"永续 funding carry 计划语义必须为 {expected}")
    source_start, source_end = (_parse_time(item) for item in data["source_window"])
    partitions = tuple(
        (name, _parse_time(bounds[0]), _parse_time(bounds[1]))
        for name, bounds in data["partitions"].items()
    )
    if tuple(name for name, _, _ in partitions) != (
        "development",
        "validation",
        "blind",
    ):
        raise ValueError("永续 funding carry 分区必须依次为 development/validation/blind")
    if partitions[0][1] != source_start or partitions[-1][2] != source_end:
        raise ValueError("永续 funding carry 分区必须覆盖完整数据窗")
    if any(
        left[2] != right[1] or left[1] >= left[2]
        for left, right in pairwise(partitions)
    ) or partitions[-1][1] >= partitions[-1][2]:
        raise ValueError("永续 funding carry 分区必须连续且非空")
    plan = PerpetualFundingCarryPlan(
        plan_id=str(raw["plan_id"]),
        plan_hash=content_hash(raw),
        carry_dataset_id=str(data["carry_dataset_id"]),
        spot_dataset_id=str(data["spot_dataset_id"]),
        funding_dataset_id=str(data["funding_dataset_id"]),
        source_start=source_start,
        source_end=source_end,
        partitions=partitions,
        starting_equity=Decimal(str(rule["starting_equity_usdt"])),
        formation_days=int(rule["formation_days"]),
        holding_days=int(rule["holding_days"]),
        leg_notional_fraction=Decimal(
            str(rule["target_leg_notional_fraction_of_current_equity"])
        ),
        quantity_step=Decimal(str(rule["quantity_step"])),
        minimum_notional=Decimal(str(rule["minimum_perpetual_notional_usdt"])),
        spot_fee_bps=Decimal(str(cost["spot_fee_bps_per_side"])),
        perpetual_fee_bps=Decimal(str(cost["perpetual_fee_bps_per_side"])),
        friction_bps=Decimal(
            str(cost["spread_and_impact_allowance_bps_per_leg_side"])
        ),
        total_round_trip_bps=Decimal(
            str(cost["complete_round_trip_bps_on_equal_leg_notional"])
        ),
        maintenance_margin_fraction=Decimal(
            str(risk["perpetual_maintenance_margin_fraction"])
        ),
        validation_gate=_load_gate(evaluation["validation_gate"]),
        blind_gate=_load_gate(evaluation["blind_gate"]),
    )
    if (
        plan.starting_equity != Decimal("10000")
        or plan.formation_days != 30
        or plan.holding_days != 30
        or plan.leg_notional_fraction != Decimal("0.25")
        or plan.quantity_step != Decimal("0.001")
    ):
        raise ValueError("永续 funding carry 冻结的资金、周期或数量规则发生漂移")
    calculated_cost = (
        Decimal("2") * plan.spot_fee_bps
        + Decimal("2") * plan.perpetual_fee_bps
        + Decimal("4") * plan.friction_bps
    )
    if calculated_cost != plan.total_round_trip_bps:
        raise ValueError("永续 funding carry 逐边成本与总成本不一致")
    if plan.validation_gate.subperiod_count != 4 or plan.blind_gate.subperiod_count != 4:
        raise ValueError("永续 funding carry 必须保留四个固定子阶段")
    return plan


def evaluate_perpetual_funding_carry(
    *,
    plan: PerpetualFundingCarryPlan,
    spot_dataset: HistoricalDataset,
    carry_dataset: HistoricalCarryDataset,
    plan_commit: str,
    evaluator_commit: str,
    evaluated_at: datetime,
    reveal_blind: bool,
) -> PerpetualFundingCarryResult:
    evaluated_at = require_utc(evaluated_at)
    _validate_inputs(plan, spot_dataset=spot_dataset, carry_dataset=carry_dataset)
    spot_by_time = {item.open_time: item for item in spot_dataset.bars}
    carry_by_time = {item.open_time: item for item in carry_dataset.bars}
    settlements = carry_dataset.settlements
    partition_by_name = {name: (start, end) for name, start, end in plan.partitions}

    def evaluate_partition(name: str, gate: EvaluationGate):
        start, end = partition_by_name[name]
        return _evaluate_partition(
            plan=plan,
            partition=name,
            start=start,
            end=end,
            gate=gate,
            spot_by_time=spot_by_time,
            carry_by_time=carry_by_time,
            settlements=settlements,
        )

    development = evaluate_partition("development", plan.validation_gate)
    validation = evaluate_partition("validation", plan.validation_gate)
    if validation.gate_reasons:
        status = "REJECTED_VALIDATION"
        reasons = tuple(f"VALIDATION::{item}" for item in validation.gate_reasons)
        blind = None
    elif not reveal_blind:
        status = "VALIDATION_PASSED_BLIND_RESERVED"
        reasons = ("VALIDATION_GATES_PASSED", "BLIND_NOT_REVEALED")
        blind = None
    else:
        blind = evaluate_partition("blind", plan.blind_gate)
        if blind.gate_reasons:
            status = "REJECTED_BLIND"
            reasons = tuple(f"BLIND::{item}" for item in blind.gate_reasons)
        else:
            status = "PASSED_RETROSPECTIVE"
            reasons = ("ALL_PREREGISTERED_GATES_PASSED",)
    blind_revealed = blind is not None
    identity = {
        "plan_hash": plan.plan_hash,
        "carry_dataset_id": carry_dataset.manifest.dataset_id,
        "spot_dataset_id": spot_dataset.manifest.dataset_id,
        "evaluator_commit": evaluator_commit,
        "blind_revealed": blind_revealed,
        "development": development,
        "validation": validation,
        "blind": blind,
    }
    return PerpetualFundingCarryResult(
        result_id=stable_id("perpetual_funding_carry_result", content_hash(identity)),
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        plan_commit=plan_commit,
        evaluator_commit=evaluator_commit,
        carry_dataset_id=carry_dataset.manifest.dataset_id,
        spot_dataset_id=spot_dataset.manifest.dataset_id,
        funding_dataset_id=carry_dataset.manifest.funding_dataset_id or "",
        evaluated_at=evaluated_at,
        blind_revealed=blind_revealed,
        status=status,
        reason_codes=reasons,
        development=development,
        validation=validation,
        blind=blind,
        limitations=(
            "历史日线没有可执行 bid/ask，冻结摩擦只能否决，不能授权资本。",
            "现货盈利未跨钱包补充永续保证金，保留真实场所路径风险。",
            "开发段不调参；validation 或 blind 揭示后禁止搜索相邻周期、门槛或仓位。",
        ),
    )


def store_perpetual_funding_carry_result(
    result: PerpetualFundingCarryResult,
    *,
    target: Path,
) -> Path:
    if target.exists():
        existing = PerpetualFundingCarryResult.model_validate_json(
            target.read_text(encoding="utf-8")
        )
        if existing != result:
            raise ValueError("永续 funding carry 结果目标已存在不同内容")
        return target
    return write_json_artifact(
        root=target.parent,
        target=target,
        prefix=".perpetual-funding-carry-result-",
        payload=result,
    )


def _evaluate_partition(
    *,
    plan: PerpetualFundingCarryPlan,
    partition: str,
    start: datetime,
    end: datetime,
    gate: EvaluationGate,
    spot_by_time: dict[datetime, ClosedMarketBar],
    carry_by_time: dict[datetime, CarryMarketBar],
    settlements: tuple[CarryFundingSettlement, ...],
) -> FundingCarryPartitionOutcome:
    equity = plan.starting_equity
    high_water = equity
    maximum_drawdown = Decimal("0")
    windows: list[FundingCarryWindowOutcome] = []
    cursor = start + timedelta(days=plan.formation_days)
    while cursor + timedelta(days=plan.holding_days) <= end:
        outcome, path = _evaluate_window(
            plan=plan,
            partition=partition,
            entry_time=cursor,
            exit_time=cursor + timedelta(days=plan.holding_days),
            equity_before=equity,
            spot_by_time=spot_by_time,
            carry_by_time=carry_by_time,
            settlements=settlements,
        )
        for marked in path:
            high_water = max(high_water, marked)
            if high_water > 0:
                maximum_drawdown = max(
                    maximum_drawdown,
                    (high_water - marked) / high_water,
                )
        equity = outcome.equity_after
        high_water = max(high_water, equity)
        windows.append(outcome)
        cursor += timedelta(days=plan.holding_days)
    entered = tuple(item for item in windows if item.status in {"ENTERED", "LIQUIDATED"})
    positive_fraction = (
        None
        if not entered
        else Decimal(sum(item.net_pnl > 0 for item in entered)) / Decimal(len(entered))
    )
    minimum_margin = (
        min(
            item.minimum_margin_buffer_fraction
            for item in entered
            if item.minimum_margin_buffer_fraction is not None
        )
        if entered
        else None
    )
    subperiods = _subperiods(
        start=start,
        end=end,
        count=gate.subperiod_count,
        windows=tuple(windows),
    )
    liquidations = sum(item.status == "LIQUIDATED" for item in windows)
    net_return = equity / plan.starting_equity - 1
    reasons: list[str] = []
    if any(item.status == "INCOMPLETE" for item in windows):
        reasons.append("INCOMPLETE_WINDOWS_PRESENT")
    if len(entered) < gate.minimum_entered_windows:
        reasons.append("MINIMUM_ENTERED_WINDOWS_NOT_MET")
    if positive_fraction is None or positive_fraction < gate.minimum_positive_window_fraction:
        reasons.append("POSITIVE_WINDOW_FRACTION_BELOW_GATE")
    if sum(item.net_return_fraction > 0 for item in subperiods) < gate.minimum_positive_subperiods:
        reasons.append("POSITIVE_SUBPERIOD_COUNT_BELOW_GATE")
    if net_return <= gate.minimum_net_return_fraction:
        reasons.append("ACCOUNT_NET_RETURN_NOT_POSITIVE")
    if maximum_drawdown > gate.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if liquidations != gate.liquidation_count:
        reasons.append("LIQUIDATION_OCCURRED")
    return FundingCarryPartitionOutcome(
        partition=partition,
        start=start,
        end=end,
        eligible_window_count=len(windows),
        entered_window_count=len(entered),
        positive_entered_window_fraction=positive_fraction,
        starting_equity=plan.starting_equity,
        ending_equity=equity,
        net_return_fraction=net_return,
        funding_pnl=sum((item.funding_pnl for item in entered), Decimal("0")),
        basis_and_price_pnl=sum(
            (item.basis_and_price_pnl for item in entered), Decimal("0")
        ),
        modeled_cost=sum((item.modeled_cost for item in entered), Decimal("0")),
        maximum_drawdown_fraction=maximum_drawdown,
        minimum_margin_buffer_fraction=minimum_margin,
        liquidation_count=liquidations,
        subperiods=subperiods,
        gate_reasons=tuple(reasons),
        windows=tuple(windows),
    )


def _evaluate_window(
    *,
    plan: PerpetualFundingCarryPlan,
    partition: str,
    entry_time: datetime,
    exit_time: datetime,
    equity_before: Decimal,
    spot_by_time: dict[datetime, ClosedMarketBar],
    carry_by_time: dict[datetime, CarryMarketBar],
    settlements: tuple[CarryFundingSettlement, ...],
) -> tuple[FundingCarryWindowOutcome, tuple[Decimal, ...]]:
    spot_entry_bar = spot_by_time.get(entry_time)
    spot_exit_bar = spot_by_time.get(exit_time)
    perpetual_entry_bar = carry_by_time.get(entry_time)
    perpetual_exit_bar = carry_by_time.get(exit_time)
    expected = tuple(
        entry_time + index * _DAY for index in range(plan.holding_days)
    )
    missing = tuple(
        item
        for item in expected
        if item not in spot_by_time or item not in carry_by_time
    )
    formation_start = entry_time - timedelta(days=plan.formation_days)
    trailing = tuple(
        item
        for item in settlements
        if formation_start <= item.funding_time < entry_time
        and item.available_at <= entry_time
    )
    trailing_fraction = sum((item.funding_rate for item in trailing), Decimal("0"))
    if (
        spot_entry_bar is None
        or spot_exit_bar is None
        or perpetual_entry_bar is None
        or perpetual_exit_bar is None
        or missing
    ):
        return (
            FundingCarryWindowOutcome(
                partition=partition,
                entry_time=entry_time,
                exit_time=exit_time,
                status="INCOMPLETE",
                reason=f"MISSING_COMMON_DAILY_BARS::{len(missing)}",
                equity_before=equity_before,
                equity_after=equity_before,
                trailing_funding_fraction=trailing_fraction,
                quantity=Decimal("0"),
                funding_pnl=Decimal("0"),
                basis_and_price_pnl=Decimal("0"),
                modeled_cost=Decimal("0"),
                net_pnl=Decimal("0"),
                return_fraction=Decimal("0"),
            ),
            (equity_before,),
        )
    raw_spot_entry = spot_entry_bar.open
    raw_perpetual_entry = perpetual_entry_bar.contract_open
    if trailing_fraction <= plan.total_round_trip_bps / _BPS:
        return (
            FundingCarryWindowOutcome(
                partition=partition,
                entry_time=entry_time,
                exit_time=exit_time,
                status="SKIPPED_COST",
                reason="TRAILING_FUNDING_DID_NOT_EXCEED_FULL_COST",
                equity_before=equity_before,
                equity_after=equity_before,
                trailing_funding_fraction=trailing_fraction,
                quantity=Decimal("0"),
                spot_entry=raw_spot_entry,
                perpetual_entry=raw_perpetual_entry,
                funding_pnl=Decimal("0"),
                basis_and_price_pnl=Decimal("0"),
                modeled_cost=Decimal("0"),
                net_pnl=Decimal("0"),
                return_fraction=Decimal("0"),
            ),
            (equity_before,),
        )
    friction = plan.friction_bps / _BPS
    spot_entry = raw_spot_entry * (1 + friction)
    perpetual_entry = raw_perpetual_entry * (1 - friction)
    quantity = _floor_quantity(
        min(
            equity_before * plan.leg_notional_fraction / spot_entry,
            equity_before * plan.leg_notional_fraction / perpetual_entry,
        ),
        plan.quantity_step,
    )
    if quantity * perpetual_entry < plan.minimum_notional:
        return (
            FundingCarryWindowOutcome(
                partition=partition,
                entry_time=entry_time,
                exit_time=exit_time,
                status="INCOMPLETE",
                reason="MINIMUM_NOTIONAL_NOT_MET",
                equity_before=equity_before,
                equity_after=equity_before,
                trailing_funding_fraction=trailing_fraction,
                quantity=Decimal("0"),
                spot_entry=raw_spot_entry,
                perpetual_entry=raw_perpetual_entry,
                funding_pnl=Decimal("0"),
                basis_and_price_pnl=Decimal("0"),
                modeled_cost=Decimal("0"),
                net_pnl=Decimal("0"),
                return_fraction=Decimal("0"),
            ),
            (equity_before,),
        )
    entry_fees = quantity * (
        spot_entry * plan.spot_fee_bps
        + perpetual_entry * plan.perpetual_fee_bps
    ) / _BPS
    wallet_cash = equity_before - quantity * spot_entry - entry_fees
    held_settlements = tuple(
        item for item in settlements if entry_time < item.funding_time < exit_time
    )
    path: list[Decimal] = []
    minimum_buffer = Decimal("Infinity")
    local_high = equity_before
    local_drawdown = Decimal("0")
    liquidation_time: datetime | None = None
    for at in expected:
        spot = spot_by_time[at]
        perpetual = carry_by_time[at]
        visible_funding = sum(
            (
                quantity * item.mark_price * item.funding_rate
                for item in held_settlements
                if item.funding_time <= perpetual.close_time
            ),
            Decimal("0"),
        )
        margin_equity = (
            wallet_cash
            + quantity * (perpetual_entry - perpetual.contract_close)
            + visible_funding
        )
        maintenance = (
            quantity
            * perpetual.contract_close
            * plan.maintenance_margin_fraction
        )
        buffer = (margin_equity - maintenance) / equity_before
        minimum_buffer = min(minimum_buffer, buffer)
        marked_spot = spot.close * (1 - friction)
        marked_perpetual = perpetual.contract_close * (1 + friction)
        exit_fees = quantity * (
            marked_spot * plan.spot_fee_bps
            + marked_perpetual * plan.perpetual_fee_bps
        ) / _BPS
        marked_equity = max(
            Decimal("0"),
            quantity * marked_spot
            + wallet_cash
            + quantity * (perpetual_entry - marked_perpetual)
            + visible_funding
            - exit_fees,
        )
        path.append(marked_equity)
        local_high = max(local_high, marked_equity)
        if local_high > 0:
            local_drawdown = max(
                local_drawdown,
                (local_high - marked_equity) / local_high,
            )
        if margin_equity < maintenance:
            liquidation_time = at
            break
    if liquidation_time is not None:
        spot_exit_bar = spot_by_time[liquidation_time]
        perpetual_liquidation_bar = carry_by_time[liquidation_time]
        return _complete_window(
            plan=plan,
            partition=partition,
            entry_time=entry_time,
            exit_time=exit_time,
            status="LIQUIDATED",
            reason=f"MARGIN_MAINTENANCE_BREACHED_AT::{liquidation_time.isoformat()}",
            equity_before=equity_before,
            trailing_fraction=trailing_fraction,
            quantity=quantity,
            raw_spot_entry=raw_spot_entry,
            raw_perpetual_entry=raw_perpetual_entry,
            raw_spot_exit=spot_exit_bar.close,
            raw_perpetual_exit=perpetual_liquidation_bar.contract_close,
            settlements=tuple(
                item
                for item in held_settlements
                if item.funding_time <= perpetual_liquidation_bar.close_time
            ),
            minimum_buffer=minimum_buffer,
            maximum_drawdown=local_drawdown,
            path=tuple(path),
        )
    return _complete_window(
        plan=plan,
        partition=partition,
        entry_time=entry_time,
        exit_time=exit_time,
        status="ENTERED",
        reason=None,
        equity_before=equity_before,
        trailing_fraction=trailing_fraction,
        quantity=quantity,
        raw_spot_entry=raw_spot_entry,
        raw_perpetual_entry=raw_perpetual_entry,
        raw_spot_exit=spot_exit_bar.open,
        raw_perpetual_exit=perpetual_exit_bar.contract_open,
        settlements=held_settlements,
        minimum_buffer=minimum_buffer,
        maximum_drawdown=local_drawdown,
        path=tuple(path),
    )


def _complete_window(
    *,
    plan: PerpetualFundingCarryPlan,
    partition: str,
    entry_time: datetime,
    exit_time: datetime,
    status: Literal["ENTERED", "LIQUIDATED"],
    reason: str | None,
    equity_before: Decimal,
    trailing_fraction: Decimal,
    quantity: Decimal,
    raw_spot_entry: Decimal,
    raw_perpetual_entry: Decimal,
    raw_spot_exit: Decimal,
    raw_perpetual_exit: Decimal,
    settlements: tuple[CarryFundingSettlement, ...],
    minimum_buffer: Decimal,
    maximum_drawdown: Decimal,
    path: tuple[Decimal, ...],
) -> tuple[FundingCarryWindowOutcome, tuple[Decimal, ...]]:
    friction = plan.friction_bps / _BPS
    spot_entry = raw_spot_entry * (1 + friction)
    perpetual_entry = raw_perpetual_entry * (1 - friction)
    spot_exit = raw_spot_exit * (1 - friction)
    perpetual_exit = raw_perpetual_exit * (1 + friction)
    funding_pnl = sum(
        (quantity * item.mark_price * item.funding_rate for item in settlements),
        Decimal("0"),
    )
    basis_and_price_pnl = quantity * (
        raw_spot_exit - raw_spot_entry
        + raw_perpetual_entry - raw_perpetual_exit
    )
    friction_cost = quantity * (
        raw_spot_entry + raw_spot_exit + raw_perpetual_entry + raw_perpetual_exit
    ) * friction
    fee_cost = quantity * (
        (spot_entry + spot_exit) * plan.spot_fee_bps
        + (perpetual_entry + perpetual_exit) * plan.perpetual_fee_bps
    ) / _BPS
    modeled_cost = friction_cost + fee_cost
    net_pnl = basis_and_price_pnl + funding_pnl - modeled_cost
    equity_after = max(Decimal("0"), equity_before + net_pnl)
    completed_path = (*path, equity_after)
    local_high = max((equity_before, *completed_path))
    final_drawdown = max(
        maximum_drawdown,
        Decimal("0") if local_high <= 0 else (local_high - equity_after) / local_high,
    )
    return (
        FundingCarryWindowOutcome(
            partition=partition,
            entry_time=entry_time,
            exit_time=exit_time,
            status=status,
            reason=reason,
            equity_before=equity_before,
            equity_after=equity_after,
            trailing_funding_fraction=trailing_fraction,
            quantity=quantity,
            spot_entry=spot_entry,
            perpetual_entry=perpetual_entry,
            spot_exit=spot_exit,
            perpetual_exit=perpetual_exit,
            funding_pnl=funding_pnl,
            basis_and_price_pnl=basis_and_price_pnl,
            modeled_cost=modeled_cost,
            net_pnl=net_pnl,
            return_fraction=net_pnl / equity_before,
            minimum_margin_buffer_fraction=minimum_buffer,
            maximum_drawdown_fraction=final_drawdown,
        ),
        completed_path,
    )


def _subperiods(
    *,
    start: datetime,
    end: datetime,
    count: int,
    windows: tuple[FundingCarryWindowOutcome, ...],
) -> tuple[FundingCarrySubperiodOutcome, ...]:
    duration = end - start
    outcomes: list[FundingCarrySubperiodOutcome] = []
    for index in range(count):
        left = start + duration * index / count
        right = start + duration * (index + 1) / count
        selected = tuple(item for item in windows if left <= item.entry_time < right)
        entered = tuple(
            item for item in selected if item.status in {"ENTERED", "LIQUIDATED"}
        )
        net_return = (
            Decimal("0")
            if not selected
            else selected[-1].equity_after / selected[0].equity_before - 1
        )
        outcomes.append(
            FundingCarrySubperiodOutcome(
                start=left,
                end=right,
                entered_window_count=len(entered),
                net_return_fraction=net_return,
            )
        )
    return tuple(outcomes)


def _validate_inputs(
    plan: PerpetualFundingCarryPlan,
    *,
    spot_dataset: HistoricalDataset,
    carry_dataset: HistoricalCarryDataset,
) -> None:
    spot = spot_dataset.manifest
    carry = carry_dataset.manifest
    if (
        spot.dataset_id != plan.spot_dataset_id
        or carry.dataset_id != plan.carry_dataset_id
        or carry.funding_dataset_id != plan.funding_dataset_id
        or carry.spot_dataset_id != spot.dataset_id
    ):
        raise ValueError("永续 funding carry 数据集身份与预注册计划不一致")
    if (
        spot.symbol != "BTCUSDT"
        or carry.symbol != "BTCUSDT"
        or spot.interval != "1d"
        or carry.interval != "1d"
        or spot.requested_start != plan.source_start
        or spot.requested_end != plan.source_end
        or carry.requested_start != plan.source_start
        or carry.requested_end != plan.source_end
    ):
        raise ValueError("永续 funding carry 数据边界与预注册计划不一致")
    spot_times = tuple(item.open_time for item in spot_dataset.bars)
    carry_times = tuple(item.open_time for item in carry_dataset.bars)
    if spot_times != carry_times or len(spot_times) != len(set(spot_times)):
        raise ValueError("永续 funding carry 现货与合约日线未精确对齐")
    if not carry_dataset.settlements:
        raise ValueError("永续 funding carry 缺少已验证逐次 funding")


def _load_gate(raw: Any) -> EvaluationGate:
    if not isinstance(raw, dict):
        raise ValueError("永续 funding carry 评价门必须是对象")
    return EvaluationGate(
        minimum_entered_windows=int(raw["minimum_entered_windows"]),
        minimum_positive_window_fraction=Decimal(
            str(raw["minimum_positive_window_fraction"])
        ),
        minimum_positive_subperiods=int(raw["minimum_positive_subperiods"]),
        subperiod_count=int(raw["subperiod_count"]),
        minimum_net_return_fraction=Decimal(str(raw["minimum_net_return_fraction"])),
        maximum_drawdown_fraction=Decimal(str(raw["maximum_drawdown_fraction"])),
        liquidation_count=int(raw["liquidation_count"]),
    )


def _floor_quantity(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return require_utc(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return require_utc(parsed.astimezone(UTC))
