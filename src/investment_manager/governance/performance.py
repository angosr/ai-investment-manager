from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.lifecycle import PositionLifecycleManager
from investment_manager.execution.reconciliation import MockReconciler
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.legacy.cycle import AnalysisCycle, CycleInput
from investment_manager.legacy.models import (
    CycleOutcome,
    DecisionOutcome,
)
from investment_manager.market.models import MarketSnapshot


class ReplayCase(FrozenModel):
    case_id: str
    cycle_input: CycleInput
    outcome_market: MarketSnapshot

    @model_validator(mode="after")
    def outcome_is_forward_and_same_symbol(self):
        if self.outcome_market.as_of <= self.cycle_input.market.as_of:
            raise ValueError("outcome_market 必须晚于分析快照")
        if self.outcome_market.symbol != self.cycle_input.market.symbol:
            raise ValueError("outcome_market 与分析品种必须一致")
        return self


class VariantMetrics(FrozenModel):
    variant_id: str
    sample_size: int = Field(ge=0)
    executed_count: int = Field(ge=0)
    no_action_count: int = Field(ge=0)
    no_trade_count: int = Field(ge=0)
    risk_rejected_count: int = Field(ge=0)
    closed_trade_count: int = Field(ge=0)
    winning_trade_count: int = Field(ge=0)
    gross_pnl: Decimal
    total_fees: Decimal = Field(ge=0)
    net_pnl: Decimal
    maximum_drawdown: Decimal = Field(ge=0)
    win_rate: Decimal = Field(ge=0, le=1)


class PairwiseComparison(FrozenModel):
    challenger_id: str
    baseline_id: str
    incremental_net_pnl: Decimal
    incremental_executions: int


class ReplayEvaluationReport(FrozenModel):
    report_id: str
    evaluation_version: str
    dataset_hash: str
    variants: tuple[VariantMetrics, ...]
    comparisons: tuple[PairwiseComparison, ...]
    statistically_conclusive: bool
    limitations: tuple[str, ...]


class OutcomeWindowStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class CycleDecisionFact(FrozenModel):
    cycle_id: str
    pipeline_version: str
    outcome: CycleOutcome
    reason_code: str


class OutcomeMetrics(FrozenModel):
    gross_pnl: Decimal
    total_fees: Decimal = Field(ge=0)
    net_pnl: Decimal
    gross_profit: Decimal = Field(ge=0)
    gross_loss: Decimal = Field(ge=0)
    profit_factor: Decimal | None = Field(default=None, ge=0)
    maximum_drawdown: Decimal = Field(ge=0)
    closed_trade_count: int = Field(ge=0)
    winning_trade_count: int = Field(ge=0)
    win_rate: Decimal = Field(ge=0, le=1)


def calculate_outcome_metrics(
    outcomes: tuple[DecisionOutcome, ...],
) -> OutcomeMetrics:
    ordered = tuple(sorted(outcomes, key=lambda item: (item.closed_at, item.outcome_id)))
    gross = sum((item.gross_pnl for item in ordered), Decimal("0"))
    fees = sum((item.total_fees for item in ordered), Decimal("0"))
    net = sum((item.net_pnl for item in ordered), Decimal("0"))
    gross_profit = sum((item.net_pnl for item in ordered if item.net_pnl > 0), Decimal("0"))
    gross_loss = -sum((item.net_pnl for item in ordered if item.net_pnl < 0), Decimal("0"))
    equity = peak = maximum_drawdown = Decimal("0")
    for item in ordered:
        equity += item.net_pnl
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    wins = sum(item.net_pnl > 0 for item in ordered)
    closed = len(ordered)
    return OutcomeMetrics(
        gross_pnl=gross,
        total_fees=fees,
        net_pnl=net,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        maximum_drawdown=maximum_drawdown,
        closed_trade_count=closed,
        winning_trade_count=wins,
        win_rate=Decimal(wins) / Decimal(closed) if closed else Decimal("0"),
    )


class OutcomeWindowReport(FrozenModel):
    report_id: str
    evaluation_version: str
    pipeline_version: str
    window_start: datetime
    window_end: datetime
    status: OutcomeWindowStatus
    source_hash: str
    cycle_count: int = Field(ge=0)
    outcome_counts: tuple[tuple[str, int], ...]
    reason_counts: tuple[tuple[str, int], ...]
    closed_trade_count: int = Field(ge=0)
    winning_trade_count: int = Field(ge=0)
    unresolved_cycle_ids: tuple[str, ...] = ()
    gross_pnl: Decimal
    total_fees: Decimal = Field(ge=0)
    net_pnl: Decimal
    gross_profit: Decimal = Field(ge=0)
    gross_loss: Decimal = Field(ge=0)
    profit_factor: Decimal | None = Field(default=None, ge=0)
    maximum_drawdown: Decimal = Field(ge=0)
    win_rate: Decimal = Field(ge=0, le=1)
    incremental_net_pnl_vs_never_trade: Decimal
    outcome_ids: tuple[str, ...] = ()

    _utc_window_start = field_validator("window_start")(require_utc)
    _utc_window_end = field_validator("window_end")(require_utc)

    @model_validator(mode="after")
    def identity_and_completion_must_match(self):
        if self.window_end <= self.window_start:
            raise ValueError("结果窗口结束时间必须晚于开始时间")
        if (self.status == OutcomeWindowStatus.COMPLETE) == bool(self.unresolved_cycle_ids):
            raise ValueError("窗口完整状态与未决周期不一致")
        expected_id = stable_id(
            "outcome_window",
            self.evaluation_version,
            self.pipeline_version,
            self.window_start.isoformat(),
            self.window_end.isoformat(),
            self.source_hash,
        )
        if self.report_id != expected_id:
            raise ValueError("OutcomeWindowReport report_id 不一致")
        return self


class OutcomeWindowEvaluator:
    def __init__(self, *, version: str) -> None:
        self._version = version

    def evaluate(
        self,
        *,
        pipeline_version: str,
        window_start: datetime,
        window_end: datetime,
        cycles: tuple[CycleDecisionFact, ...],
        outcomes: tuple[DecisionOutcome, ...],
        unresolved_cycle_ids: tuple[str, ...],
    ) -> OutcomeWindowReport:
        window_start = require_utc(window_start)
        window_end = require_utc(window_end)
        if window_end <= window_start:
            raise ValueError("结果窗口结束时间必须晚于开始时间")
        if any(item.pipeline_version != pipeline_version for item in cycles):
            raise ValueError("结果窗口包含其他 Pipeline 的周期")
        cycle_ids = {item.cycle_id for item in cycles}
        if len(cycle_ids) != len(cycles):
            raise ValueError("结果窗口 cycle_id 不得重复")
        if not set(unresolved_cycle_ids).issubset(cycle_ids):
            raise ValueError("未决周期必须属于当前窗口")
        if any(item.cycle_id not in cycle_ids for item in outcomes):
            raise ValueError("逐笔结果必须属于当前窗口周期")

        ordered_outcomes = tuple(
            sorted(outcomes, key=lambda item: (item.closed_at, item.outcome_id))
        )
        metrics = calculate_outcome_metrics(ordered_outcomes)
        outcome_counts = tuple(
            (outcome.value, sum(item.outcome == outcome for item in cycles))
            for outcome in CycleOutcome
        )
        reason_counts = tuple(
            (reason, sum(item.reason_code == reason for item in cycles))
            for reason in sorted({item.reason_code for item in cycles})
        )
        normalized_unresolved = tuple(sorted(set(unresolved_cycle_ids)))
        source_payload = {
            "cycles": [item.model_dump(mode="json") for item in cycles],
            "outcomes": [item.model_dump(mode="json") for item in ordered_outcomes],
            "unresolved_cycle_ids": normalized_unresolved,
        }
        source_hash = content_hash(source_payload)
        report_id = stable_id(
            "outcome_window",
            self._version,
            pipeline_version,
            window_start.isoformat(),
            window_end.isoformat(),
            source_hash,
        )
        return OutcomeWindowReport(
            report_id=report_id,
            evaluation_version=self._version,
            pipeline_version=pipeline_version,
            window_start=window_start,
            window_end=window_end,
            status=(
                OutcomeWindowStatus.INCOMPLETE
                if normalized_unresolved
                else OutcomeWindowStatus.COMPLETE
            ),
            source_hash=source_hash,
            cycle_count=len(cycles),
            outcome_counts=outcome_counts,
            reason_counts=reason_counts,
            closed_trade_count=metrics.closed_trade_count,
            winning_trade_count=metrics.winning_trade_count,
            unresolved_cycle_ids=normalized_unresolved,
            gross_pnl=metrics.gross_pnl,
            total_fees=metrics.total_fees,
            net_pnl=metrics.net_pnl,
            gross_profit=metrics.gross_profit,
            gross_loss=metrics.gross_loss,
            profit_factor=metrics.profit_factor,
            maximum_drawdown=metrics.maximum_drawdown,
            win_rate=metrics.win_rate,
            incremental_net_pnl_vs_never_trade=metrics.net_pnl,
            outcome_ids=tuple(item.outcome_id for item in ordered_outcomes),
        )


CycleFactory = Callable[[], AnalysisCycle]
InputTransform = Callable[[CycleInput], CycleInput]


def identity_input(value: CycleInput) -> CycleInput:
    return value


@dataclass(frozen=True, slots=True)
class EvaluationVariant:
    cycle_factory: CycleFactory
    input_transform: InputTransform = identity_input


class ReplayEvaluator:
    def __init__(
        self,
        *,
        version: str = "replay-evaluation-v1",
        minimum_conclusive_sample: int = 100,
    ) -> None:
        self._version = version
        self._minimum_conclusive_sample = minimum_conclusive_sample

    def evaluate(
        self,
        cases: tuple[ReplayCase, ...],
        variants: dict[str, EvaluationVariant | CycleFactory | None],
        *,
        baseline_ids: tuple[str, ...] = ("never_trade",),
    ) -> ReplayEvaluationReport:
        if not cases:
            raise ValueError("评估集不能为空")
        if len({item.case_id for item in cases}) != len(cases):
            raise ValueError("ReplayCase case_id 必须唯一")
        if len(variants) < 2 or "never_trade" not in variants:
            raise ValueError("至少需要 never_trade 和一个候选变体")
        if not baseline_ids or not set(baseline_ids).issubset(variants):
            raise ValueError("baseline_ids 必须引用已登记变体")
        dataset_hash = content_hash([item.model_dump(mode="json") for item in cases])
        metrics = tuple(
            self._evaluate_variant(variant_id, factory, cases)
            for variant_id, factory in sorted(variants.items())
        )
        by_id = {item.variant_id: item for item in metrics}
        comparisons = tuple(
            PairwiseComparison(
                challenger_id=item.variant_id,
                baseline_id=baseline.variant_id,
                incremental_net_pnl=item.net_pnl - baseline.net_pnl,
                incremental_executions=item.executed_count - baseline.executed_count,
            )
            for baseline_id in baseline_ids
            for baseline in (by_id[baseline_id],)
            for item in metrics
            if item.variant_id != baseline_id
        )
        conclusive = len(cases) >= self._minimum_conclusive_sample
        limitations = () if conclusive else ("SAMPLE_TOO_SMALL_FOR_STATISTICAL_CONCLUSION",)
        return ReplayEvaluationReport(
            report_id=stable_id(
                "replay_report",
                self._version,
                dataset_hash,
                content_hash([item.model_dump(mode="json") for item in metrics]),
            ),
            evaluation_version=self._version,
            dataset_hash=dataset_hash,
            variants=metrics,
            comparisons=comparisons,
            statistically_conclusive=conclusive,
            limitations=limitations,
        )

    @staticmethod
    def _evaluate_variant(
        variant_id: str,
        variant: EvaluationVariant | CycleFactory | None,
        cases: tuple[ReplayCase, ...],
    ) -> VariantMetrics:
        if variant is None:
            return VariantMetrics(
                variant_id=variant_id,
                sample_size=len(cases),
                executed_count=0,
                no_action_count=len(cases),
                no_trade_count=0,
                risk_rejected_count=0,
                closed_trade_count=0,
                winning_trade_count=0,
                gross_pnl=Decimal("0"),
                total_fees=Decimal("0"),
                net_pnl=Decimal("0"),
                maximum_drawdown=Decimal("0"),
                win_rate=Decimal("0"),
            )
        if isinstance(variant, EvaluationVariant):
            cycle = variant.cycle_factory()
            transform = variant.input_transform
        else:
            cycle = variant()
            transform = identity_input
        counts = {outcome: 0 for outcome in CycleOutcome}
        gross = fees = net = Decimal("0")
        closed = wins = 0
        equity = peak = maximum_drawdown = Decimal("0")
        for case in cases:
            result = cycle.run(transform(case.cycle_input))
            counts[result.outcome] += 1
            if result.position_lifecycle is None or result.account_after is None:
                continue
            lifecycle_result = PositionLifecycleManager(
                exchange=cycle.exchange,
                reconciler=MockReconciler(),
                risk_budget=cycle.risk_budget,
            ).evaluate(
                lifecycle=result.position_lifecycle,
                market=case.outcome_market,
                account=result.account_after,
                pipeline_version=cycle.config.pipeline.version,
            )
            if lifecycle_result.outcome is None:
                continue
            outcome = lifecycle_result.outcome
            closed += 1
            gross += outcome.gross_pnl
            fees += outcome.total_fees
            net += outcome.net_pnl
            if outcome.net_pnl > 0:
                wins += 1
            equity += outcome.net_pnl
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, peak - equity)
        return VariantMetrics(
            variant_id=variant_id,
            sample_size=len(cases),
            executed_count=counts[CycleOutcome.EXECUTED],
            no_action_count=counts[CycleOutcome.NO_ACTION],
            no_trade_count=counts[CycleOutcome.NO_TRADE],
            risk_rejected_count=counts[CycleOutcome.RISK_REJECTED],
            closed_trade_count=closed,
            winning_trade_count=wins,
            gross_pnl=gross,
            total_fees=fees,
            net_pnl=net,
            maximum_drawdown=maximum_drawdown,
            win_rate=Decimal(wins) / Decimal(closed) if closed else Decimal("0"),
        )


def without_information_events(cycle_input: CycleInput) -> CycleInput:
    return cycle_input.model_copy(update={"events": ()})
