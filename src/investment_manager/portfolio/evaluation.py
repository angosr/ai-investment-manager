"""Cost-after outcome diagnostics for one immutable portfolio choice."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from investment_manager.execution.models import Side
from investment_manager.forecast.models import ExposureDirection
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.models import CapitalCycleOutcome, CapitalCycleRecord

CAPITAL_CHOICE_EVALUATION_VERSION = "capital-choice-outcome-v4"
TRADING_COST_EVALUATION_VERSION = "trading-cost-evidence-v1"
_FORECAST_DECISION_TRIGGERS = frozenset({"FORECAST_CADENCE", "FORECAST_EVENT_DUE"})


def is_full_forecast_capital_choice(
    record: CapitalCycleRecord,
    *,
    capital_behavior_id: str,
) -> bool:
    """Whether a receipt owns a fresh Forecast-to-capital comparison.

    Holding/risk reviews may legally retain, resize, or exit current Sleeves, but
    they do not reconstruct every unheld candidate and therefore cannot support
    a cross-exposure missed-opportunity claim.
    """

    if not capital_behavior_id:
        raise ValueError("Capital choice 评价必须指定资本行为身份")
    return (
        record.pipeline_id == capital_behavior_id
        and record.outcome == CapitalCycleOutcome.TARGET_DECIDED
        and not _FORECAST_DECISION_TRIGGERS.isdisjoint(record.trigger_types)
    )


@dataclass(frozen=True, slots=True)
class CapitalChoiceCase:
    """One product candidate exactly as compared with cash at decision time."""

    decision_id: str
    decision_at: datetime
    evaluation_at: datetime
    economic_exposure_id: str
    projection_id: str
    instrument_key: str
    direction: ExposureDirection
    selected: bool
    predicted_net_bps: Decimal
    decision_gross_bps: Decimal
    projection_gross_bps: Decimal
    decision_cost_bps: Decimal
    realized_product_gross_bps: Decimal

    def __post_init__(self) -> None:
        require_utc(self.decision_at)
        require_utc(self.evaluation_at)
        if self.decision_at >= self.evaluation_at:
            raise ValueError("Capital choice 决策时间必须早于结算时间")
        for value, label in (
            (self.decision_id, "decision_id"),
            (self.economic_exposure_id, "economic_exposure_id"),
            (self.projection_id, "projection_id"),
            (self.instrument_key, "instrument_key"),
        ):
            if not value:
                raise ValueError(f"Capital choice {label} 不能为空")
        if self.decision_cost_bps < 0:
            raise ValueError("Capital choice 决策成本不能为负数")
        if self.predicted_net_bps != self.decision_gross_bps - self.decision_cost_bps:
            raise ValueError("Capital choice 预测净收益必须等于决策毛收益减冻结成本")

    @property
    def realized_remaining_gross_bps(self) -> Decimal:
        """Re-anchor the product outcome from its entry anchor to decision time."""

        return self.realized_product_gross_bps + self.decision_gross_bps - self.projection_gross_bps

    @property
    def realized_net_bps(self) -> Decimal:
        return self.realized_remaining_gross_bps - self.decision_cost_bps


@dataclass(frozen=True, slots=True)
class CapitalChoiceCandidateOutcome:
    projection_id: str
    instrument_key: str
    direction: ExposureDirection
    predicted_net_bps: Decimal
    realized_net_bps: Decimal


@dataclass(frozen=True, slots=True)
class CapitalChoiceExposureOutcome:
    economic_exposure_id: str
    selected: CapitalChoiceCandidateOutcome | None
    best_realized: CapitalChoiceCandidateOutcome
    opportunity_gap_bps: Decimal
    missed_profitable_exposure: bool
    selected_unprofitable_exposure: bool


@dataclass(frozen=True, slots=True)
class CapitalChoiceEvidence:
    evaluation_version: str
    capital_behavior_id: str
    decision_id: str
    decision_at: datetime
    evaluation_at: datetime
    candidate_count: int
    exposures: tuple[CapitalChoiceExposureOutcome, ...]

    @property
    def missed_profitable_exposure_count(self) -> int:
        return sum(item.missed_profitable_exposure for item in self.exposures)

    @property
    def selected_unprofitable_exposure_count(self) -> int:
        return sum(item.selected_unprofitable_exposure for item in self.exposures)


@dataclass(frozen=True, slots=True)
class ExecutionFillCase:
    """One final fill projected from the authoritative execution group ledger."""

    fill_id: str
    cycle_id: str
    sleeve_id: str
    instrument_key: str
    side: Side
    group_started_at: datetime
    filled_at: datetime
    quantity: Decimal
    price: Decimal
    contract_multiplier: Decimal
    fee: Decimal

    def __post_init__(self) -> None:
        require_utc(self.group_started_at)
        require_utc(self.filled_at)
        if not all((self.fill_id, self.cycle_id, self.sleeve_id, self.instrument_key)):
            raise ValueError("Execution fill 身份不能为空")
        if min(self.quantity, self.price, self.contract_multiplier) <= 0 or self.fee < 0:
            raise ValueError("Execution fill 数量、价格、乘数或费用非法")
        if self.filled_at < self.group_started_at:
            raise ValueError("Execution fill 成交时间不能早于执行组启动")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price * self.contract_multiplier


@dataclass(frozen=True, slots=True)
class TradingRoundTrip:
    """A LIFO-matched exposure lot; this is diagnostic attribution, not tax accounting."""

    round_trip_id: str
    sleeve_id: str
    instrument_key: str
    direction: ExposureDirection
    entry_fill_id: str
    exit_fill_id: str
    entry_cycle_id: str
    exit_cycle_id: str
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    gross_turnover: Decimal
    realized_gross_pnl: Decimal
    fee_cost: Decimal
    realized_net_pnl: Decimal

    @property
    def holding_seconds(self) -> Decimal:
        return Decimal(str((self.closed_at - self.opened_at).total_seconds()))


@dataclass(frozen=True, slots=True)
class TradingCostEvidence:
    evaluation_version: str
    fill_count: int
    round_trip_count: int
    open_lot_count: int
    gross_turnover: Decimal
    realized_gross_pnl: Decimal
    closed_fee_cost: Decimal
    open_fee_cost: Decimal
    realized_net_pnl: Decimal
    positive_gross_pnl: Decimal
    closed_fee_to_realized_gross_pnl: Decimal | None
    closed_fee_to_positive_gross_pnl: Decimal | None
    cost_reversal_round_trip_count: int
    minimum_holding_seconds: Decimal | None
    median_holding_seconds: Decimal | None
    maximum_holding_seconds: Decimal | None
    accounting_reconciled: bool | None
    round_trips: tuple[TradingRoundTrip, ...]


@dataclass(slots=True)
class _OpenExecutionLot:
    fill: ExecutionFillCase
    remaining_quantity: Decimal
    remaining_fee: Decimal


def evaluate_trading_cost(
    fills: tuple[ExecutionFillCase, ...],
    *,
    expected_price_pnl: Decimal | None = None,
    expected_fee_cost: Decimal | None = None,
) -> TradingCostEvidence:
    """Reconstruct recent-exposure round trips without inventing a trading threshold."""

    fill_ids = tuple(item.fill_id for item in fills)
    if (expected_price_pnl is None) != (expected_fee_cost is None):
        raise ValueError("Trading cost 账户核对输入必须同时提供")
    if len(set(fill_ids)) != len(fill_ids):
        raise ValueError("Trading cost fill 身份不得重复")
    ordered = tuple(
        sorted(
            fills,
            key=lambda item: (item.group_started_at, item.filled_at, item.fill_id),
        )
    )
    open_lots: dict[tuple[str, str], list[_OpenExecutionLot]] = defaultdict(list)
    round_trips: list[TradingRoundTrip] = []
    for fill in ordered:
        key = (fill.sleeve_id, fill.instrument_key)
        lots = open_lots[key]
        remaining_quantity = fill.quantity
        remaining_fee = fill.fee
        fill_sign = Decimal("1") if fill.side == Side.BUY else Decimal("-1")
        while lots and remaining_quantity > 0:
            lot = lots[-1]
            lot_sign = Decimal("1") if lot.fill.side == Side.BUY else Decimal("-1")
            if lot_sign == fill_sign:
                break
            if lot.fill.contract_multiplier != fill.contract_multiplier:
                raise ValueError("同一 Sleeve/Instrument 的合约乘数发生变化")
            matched = min(lot.remaining_quantity, remaining_quantity)
            entry_fee = _allocate_amount(
                lot.remaining_fee,
                matched=matched,
                remaining=lot.remaining_quantity,
            )
            exit_fee = _allocate_amount(
                remaining_fee,
                matched=matched,
                remaining=remaining_quantity,
            )
            gross = (fill.price - lot.fill.price) * matched * fill.contract_multiplier * lot_sign
            fees = entry_fee + exit_fee
            round_trips.append(
                TradingRoundTrip(
                    round_trip_id=stable_id(
                        "trading_round_trip",
                        lot.fill.fill_id,
                        fill.fill_id,
                        str(matched),
                    ),
                    sleeve_id=fill.sleeve_id,
                    instrument_key=fill.instrument_key,
                    direction=(ExposureDirection.LONG if lot_sign > 0 else ExposureDirection.SHORT),
                    entry_fill_id=lot.fill.fill_id,
                    exit_fill_id=fill.fill_id,
                    entry_cycle_id=lot.fill.cycle_id,
                    exit_cycle_id=fill.cycle_id,
                    opened_at=lot.fill.filled_at,
                    closed_at=fill.filled_at,
                    quantity=matched,
                    gross_turnover=(
                        matched * (lot.fill.price + fill.price) * fill.contract_multiplier
                    ),
                    realized_gross_pnl=gross,
                    fee_cost=fees,
                    realized_net_pnl=gross - fees,
                )
            )
            lot.remaining_quantity -= matched
            lot.remaining_fee -= entry_fee
            remaining_quantity -= matched
            remaining_fee -= exit_fee
            if lot.remaining_quantity == 0:
                lots.pop()
        if remaining_quantity > 0:
            lots.append(
                _OpenExecutionLot(
                    fill=fill,
                    remaining_quantity=remaining_quantity,
                    remaining_fee=remaining_fee,
                )
            )

    frozen_round_trips = tuple(round_trips)
    holding_seconds = tuple(sorted(item.holding_seconds for item in frozen_round_trips))
    closed_fees = sum((item.fee_cost for item in frozen_round_trips), Decimal("0"))
    gross_pnl = sum((item.realized_gross_pnl for item in frozen_round_trips), Decimal("0"))
    positive_gross = sum(
        (max(item.realized_gross_pnl, Decimal("0")) for item in frozen_round_trips),
        Decimal("0"),
    )
    return TradingCostEvidence(
        evaluation_version=TRADING_COST_EVALUATION_VERSION,
        fill_count=len(ordered),
        round_trip_count=len(frozen_round_trips),
        open_lot_count=sum(len(items) for items in open_lots.values()),
        gross_turnover=sum((item.notional for item in ordered), Decimal("0")),
        realized_gross_pnl=gross_pnl,
        closed_fee_cost=closed_fees,
        open_fee_cost=sum(
            (lot.remaining_fee for lots in open_lots.values() for lot in lots),
            Decimal("0"),
        ),
        realized_net_pnl=gross_pnl - closed_fees,
        positive_gross_pnl=positive_gross,
        closed_fee_to_realized_gross_pnl=(closed_fees / gross_pnl if gross_pnl > 0 else None),
        closed_fee_to_positive_gross_pnl=(
            closed_fees / positive_gross if positive_gross > 0 else None
        ),
        cost_reversal_round_trip_count=sum(
            item.realized_gross_pnl > 0 and item.realized_net_pnl < 0 for item in frozen_round_trips
        ),
        minimum_holding_seconds=holding_seconds[0] if holding_seconds else None,
        median_holding_seconds=_median(holding_seconds),
        maximum_holding_seconds=holding_seconds[-1] if holding_seconds else None,
        accounting_reconciled=(
            None
            if expected_price_pnl is None or any(open_lots.values())
            else gross_pnl == expected_price_pnl and closed_fees == expected_fee_cost
        ),
        round_trips=frozen_round_trips,
    )


def _allocate_amount(amount: Decimal, *, matched: Decimal, remaining: Decimal) -> Decimal:
    return amount if matched == remaining else amount * matched / remaining


def _median(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / Decimal("2")


def evaluate_capital_choice(
    cases: tuple[CapitalChoiceCase, ...],
    *,
    capital_behavior_id: str,
) -> CapitalChoiceEvidence:
    """Compare the chosen product or cash with hindsight products per exposure.

    This is a diagnostic of the frozen decision.  The hindsight winner is never a
    forecast, benchmark, strategy, or source of capital permission.
    """

    if not cases:
        raise ValueError("Capital choice 评价至少需要一个候选")
    if not capital_behavior_id:
        raise ValueError("Capital choice 评价必须指定资本行为身份")
    decision_ids = {item.decision_id for item in cases}
    decision_times = {item.decision_at for item in cases}
    evaluation_times = {item.evaluation_at for item in cases}
    projection_ids = tuple(item.projection_id for item in cases)
    if len(decision_ids) != 1 or len(decision_times) != 1 or len(evaluation_times) != 1:
        raise ValueError("Capital choice 候选必须属于同一决策与共同结算时点")
    if len(set(projection_ids)) != len(projection_ids):
        raise ValueError("Capital choice 候选 projection 不得重复")

    by_exposure: defaultdict[str, list[CapitalChoiceCase]] = defaultdict(list)
    for case in cases:
        by_exposure[case.economic_exposure_id].append(case)

    exposures = []
    for exposure_id, candidates in sorted(by_exposure.items()):
        selected_cases = [item for item in candidates if item.selected]
        if len(selected_cases) > 1:
            raise ValueError("同一经济暴露不得选择多个产品表达")
        best = max(candidates, key=lambda item: (item.realized_net_bps, item.projection_id))
        selected_case = selected_cases[0] if selected_cases else None
        selected_net = selected_case.realized_net_bps if selected_case is not None else Decimal("0")
        opportunity_gap = max(Decimal("0"), best.realized_net_bps - selected_net)
        exposures.append(
            CapitalChoiceExposureOutcome(
                economic_exposure_id=exposure_id,
                selected=(_candidate_outcome(selected_case) if selected_case is not None else None),
                best_realized=_candidate_outcome(best),
                opportunity_gap_bps=opportunity_gap,
                missed_profitable_exposure=(selected_case is None and best.realized_net_bps > 0),
                selected_unprofitable_exposure=(
                    selected_case is not None and selected_case.realized_net_bps < 0
                ),
            )
        )

    return CapitalChoiceEvidence(
        evaluation_version=CAPITAL_CHOICE_EVALUATION_VERSION,
        capital_behavior_id=capital_behavior_id,
        decision_id=next(iter(decision_ids)),
        decision_at=next(iter(decision_times)),
        evaluation_at=next(iter(evaluation_times)),
        candidate_count=len(cases),
        exposures=tuple(exposures),
    )


def _candidate_outcome(case: CapitalChoiceCase) -> CapitalChoiceCandidateOutcome:
    return CapitalChoiceCandidateOutcome(
        projection_id=case.projection_id,
        instrument_key=case.instrument_key,
        direction=case.direction,
        predicted_net_bps=case.predicted_net_bps,
        realized_net_bps=case.realized_net_bps,
    )


__all__ = [
    "CAPITAL_CHOICE_EVALUATION_VERSION",
    "TRADING_COST_EVALUATION_VERSION",
    "CapitalChoiceCandidateOutcome",
    "CapitalChoiceCase",
    "CapitalChoiceEvidence",
    "CapitalChoiceExposureOutcome",
    "ExecutionFillCase",
    "TradingCostEvidence",
    "TradingRoundTrip",
    "evaluate_capital_choice",
    "evaluate_trading_cost",
    "is_full_forecast_capital_choice",
]
