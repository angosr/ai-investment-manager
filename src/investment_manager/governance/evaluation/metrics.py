from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import field_validator

from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

METRIC_VERSION = "metrics-v3"


class MetricObservation(FrozenModel):
    metric_id: str
    metric_version: str
    cycle_id: str
    observed_at: datetime
    value: Decimal
    dimensions: tuple[tuple[str, str], ...] = ()

    _utc_observed_at = field_validator("observed_at")(require_utc)


class MetricDefinition(FrozenModel):
    name: str
    domain: str
    formula: str
    source_facts: tuple[str, ...]
    window: str
    update_frequency: str


def _definition(
    name: str,
    domain: str,
    formula: str,
    sources: tuple[str, ...],
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
    )


METRIC_DEFINITIONS = {
    item.name: item
    for item in (
        _definition(
            "market_data_age_seconds",
            "runtime",
            "max(0, cycle_as_of - market_observed_at)",
            ("MarketSnapshot",),
        ),
        _definition(
            "account_data_age_seconds",
            "runtime",
            "max(0, cycle_as_of - account_observed_at)",
            ("AccountSnapshot",),
        ),
        _definition(
            "account_reconciled",
            "runtime",
            "1 if reconciled else 0",
            ("AccountSnapshot",),
        ),
        _definition(
            "codex_analysis_success",
            "ai",
            "1 if valid proposal else 0",
            ("CodexRun", "AnalysisProposal"),
        ),
        _definition(
            "codex_proposal_normalization_success",
            "ai",
            "1 if proposal maps to deterministic candidates without contract failure else 0",
            ("AnalysisProposal", "PanelSnapshot"),
        ),
        _definition(
            "signal_count",
            "decision",
            "count(valid signal candidates)",
            ("SignalCandidate",),
        ),
        _definition(
            "expected_net_edge_bps",
            "economics",
            "remaining_gross_edge - attributable_round_trip_trade_costs",
            ("TradeIntent", "FeatureSnapshot", "FrequencyPolicy"),
        ),
        _definition(
            "remaining_gross_edge_bps",
            "economics",
            "time_decayed_gross_edge - favorable_price_move_since_signal",
            ("TradeIntent", "MarketSnapshot"),
        ),
        _definition(
            "price_move_consumed_bps",
            "economics",
            "favorable_price_move_from_signal_reference_to_current_entry",
            ("TradeIntent", "MarketSnapshot"),
        ),
        _definition(
            "signal_age_seconds",
            "latency",
            "max(0, cycle_as_of - signal_observed_at)",
            ("TradeIntent",),
        ),
        _definition(
            "risk_approved",
            "risk",
            "1 if all deterministic guards pass else 0",
            ("RiskDecision",),
        ),
        _definition(
            "executed_order_count",
            "execution",
            "count(submitted entry orders)",
            ("Order",),
        ),
        _definition(
            "execution_handoff_age_seconds",
            "latency",
            "max(0, execution_observed_at - execution_request_created_at)",
            ("ExecutionRequest", "Order"),
        ),
        _definition(
            "position_protected",
            "risk",
            "1 if exchange protection registered else 0",
            ("PositionLifecycle",),
        ),
        _definition(
            "gross_pnl",
            "performance",
            "signed(exit_price - entry_price) * quantity",
            ("DecisionOutcome",),
        ),
        _definition(
            "net_pnl",
            "performance",
            "gross_pnl - entry_fee - exit_fee",
            ("DecisionOutcome",),
        ),
        _definition(
            "maximum_favorable_excursion",
            "risk",
            "best_mark_to_entry_signed_move * quantity",
            ("PositionLifecycle", "DecisionOutcome"),
        ),
        _definition(
            "maximum_adverse_excursion",
            "risk",
            "worst_mark_to_entry_signed_move * quantity",
            ("PositionLifecycle", "DecisionOutcome"),
        ),
        _definition(
            "holding_minutes",
            "execution",
            "closed_at - opened_at in minutes",
            ("DecisionOutcome",),
        ),
    )
}


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
