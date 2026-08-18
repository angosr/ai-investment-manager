from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from quant_core.domain import FrozenModel, MetricObservation
from quant_core.ids import stable_id

METRIC_VERSION = "metrics-v2"


class MetricDefinition(FrozenModel):
    name: str
    domain: str
    formula: str
    source_facts: tuple[str, ...]
    window: str
    update_frequency: str
    response: str


def _definition(
    name: str,
    domain: str,
    formula: str,
    sources: tuple[str, ...],
    response: str,
    *,
    window: str = "per_cycle",
    update_frequency: str = "per_cycle",
) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        domain=domain,
        formula=formula,
        source_facts=sources,
        window=window,
        update_frequency=update_frequency,
        response=response,
    )


METRIC_DEFINITIONS = {
    item.name: item
    for item in (
        _definition(
            "market_data_age_seconds",
            "runtime",
            "max(0, cycle_as_of - market_observed_at)",
            ("MarketSnapshot",),
            "halt_new_risk_when_stale",
        ),
        _definition(
            "account_data_age_seconds",
            "runtime",
            "max(0, cycle_as_of - account_observed_at)",
            ("AccountSnapshot",),
            "halt_new_risk_when_stale",
        ),
        _definition(
            "account_reconciled",
            "runtime",
            "1 if reconciled else 0",
            ("AccountSnapshot",),
            "halt_new_risk_when_zero",
        ),
        _definition(
            "codex_analysis_success",
            "ai",
            "1 if valid proposal else 0",
            ("CodexRun", "AnalysisProposal"),
            "propose_pipeline_no_trade_when_zero",
        ),
        _definition(
            "signal_count",
            "decision",
            "count(valid signal candidates)",
            ("SignalCandidate",),
            "observe",
        ),
        _definition(
            "expected_net_edge_bps",
            "economics",
            "remaining_gross_edge - all_in_costs",
            ("TradeIntent", "FeatureSnapshot", "FrequencyPolicy"),
            "reject_when_below_threshold",
        ),
        _definition(
            "remaining_gross_edge_bps",
            "economics",
            "time_decayed_gross_edge - favorable_price_move_since_signal",
            ("TradeIntent", "MarketSnapshot"),
            "reject_when_non_positive",
        ),
        _definition(
            "price_move_consumed_bps",
            "economics",
            "favorable_price_move_from_signal_reference_to_current_entry",
            ("TradeIntent", "MarketSnapshot"),
            "observe_alpha_consumption",
        ),
        _definition(
            "signal_age_seconds",
            "latency",
            "max(0, cycle_as_of - signal_observed_at)",
            ("TradeIntent",),
            "observe_alpha_decay",
        ),
        _definition(
            "risk_approved",
            "risk",
            "1 if all deterministic guards pass else 0",
            ("RiskDecision",),
            "block_order_when_zero",
        ),
        _definition(
            "executed_order_count",
            "execution",
            "count(submitted entry orders)",
            ("Order",),
            "observe_turnover",
        ),
        _definition(
            "position_protected",
            "risk",
            "1 if exchange protection registered else 0",
            ("PositionLifecycle",),
            "emergency_exit_when_zero",
        ),
        _definition(
            "gross_pnl",
            "performance",
            "signed(exit_price - entry_price) * quantity",
            ("DecisionOutcome",),
            "evaluate",
        ),
        _definition(
            "net_pnl",
            "performance",
            "gross_pnl - entry_fee - exit_fee",
            ("DecisionOutcome",),
            "evaluate",
        ),
        _definition(
            "maximum_favorable_excursion",
            "risk",
            "best_mark_to_entry_signed_move * quantity",
            ("PositionLifecycle", "DecisionOutcome"),
            "evaluate",
        ),
        _definition(
            "maximum_adverse_excursion",
            "risk",
            "worst_mark_to_entry_signed_move * quantity",
            ("PositionLifecycle", "DecisionOutcome"),
            "evaluate",
        ),
        _definition(
            "holding_minutes",
            "execution",
            "closed_at - opened_at in minutes",
            ("DecisionOutcome",),
            "evaluate",
        ),
    )
}


class Comparator(StrEnum):
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"


class AlertRule(FrozenModel):
    rule_id: str
    metric_name: str
    comparator: Comparator
    threshold: Decimal
    action: str


class AlertEvent(FrozenModel):
    rule_id: str
    metric_id: str
    action: str


def evaluate_alert(rule: AlertRule, metric: MetricObservation) -> AlertEvent | None:
    metric_name = dict(metric.dimensions).get("metric")
    if metric_name != rule.metric_name or metric_name not in METRIC_DEFINITIONS:
        raise ValueError("告警规则与指标定义不匹配")
    comparisons = {
        Comparator.GT: metric.value > rule.threshold,
        Comparator.GTE: metric.value >= rule.threshold,
        Comparator.LT: metric.value < rule.threshold,
        Comparator.LTE: metric.value <= rule.threshold,
        Comparator.EQ: metric.value == rule.threshold,
    }
    if not comparisons[rule.comparator]:
        return None
    return AlertEvent(rule_id=rule.rule_id, metric_id=metric.metric_id, action=rule.action)


def observation(
    name: str,
    value: Decimal | int,
    *,
    cycle_id: str,
    observed_at: datetime,
    dimensions: dict[str, str] | None = None,
) -> MetricObservation:
    if name not in METRIC_DEFINITIONS:
        raise ValueError(f"未注册的 MetricDefinition: {name}")
    normalized_dimensions = tuple(sorted((dimensions or {}).items()))
    return MetricObservation(
        metric_id=stable_id("metric", cycle_id, name, normalized_dimensions),
        metric_version=METRIC_VERSION,
        cycle_id=cycle_id,
        observed_at=observed_at,
        value=Decimal(value),
        dimensions=(("metric", name), *normalized_dimensions),
    )
