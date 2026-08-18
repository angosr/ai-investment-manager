from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_core.config import CompositionPolicy, FrequencyPolicy
from quant_core.domain import Side, SignalCandidate, TradeIntent
from quant_core.ids import stable_id


@dataclass(frozen=True, slots=True)
class CompositionResult:
    intent: TradeIntent | None
    reason_code: str


class SingleThresholdComposer:
    def __init__(self, policy: CompositionPolicy, pipeline_version: str) -> None:
        self._policy = policy
        self._pipeline_version = pipeline_version

    def compose(
        self, candidates: tuple[SignalCandidate, ...], *, as_of: datetime
    ) -> CompositionResult:
        valid = [
            candidate
            for candidate in candidates
            if candidate.valid_until > as_of
            and not candidate.unknowns
            and candidate.raw_score >= self._policy.minimum_raw_score
        ]
        if not valid:
            return CompositionResult(intent=None, reason_code="NO_VALID_CANDIDATE")
        valid.sort(key=lambda item: (-item.raw_score, item.candidate_id))
        selected = valid[0]
        intent_id = stable_id(
            "intent", selected.cycle_id, self._pipeline_version, selected.candidate_id
        )
        return CompositionResult(
            intent=TradeIntent(
                intent_id=intent_id,
                cycle_id=selected.cycle_id,
                pipeline_version=self._pipeline_version,
                composition_policy_version=self._policy.version,
                action=selected.action,
                symbol=selected.symbol,
                side=selected.side,
                candidate_ids=(selected.candidate_id,),
                entry=selected.entry,
                stop_price=selected.stop_price,
                max_holding_minutes=selected.horizon_minutes,
                valid_until=selected.valid_until,
                signal_observed_at=selected.signal_observed_at,
                reference_price=selected.reference_price,
                expected_edge_half_life_seconds=selected.expected_edge_half_life_seconds,
                expected_gross_bps=selected.expected_gross_bps,
            ),
            reason_code="COMPOSED",
        )


@dataclass(frozen=True, slots=True)
class FrequencyState:
    orders_today: int = 0
    last_order_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FrequencyDecision:
    allowed: bool
    reason_code: str
    expected_net_edge_bps: Decimal
    remaining_gross_edge_bps: Decimal
    price_move_consumed_bps: Decimal
    signal_age_seconds: Decimal


class FrequencyController:
    def __init__(self, policy: FrequencyPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        intent: TradeIntent,
        *,
        as_of: datetime,
        spread_bps: Decimal,
        current_price: Decimal,
        state: FrequencyState,
    ) -> FrequencyDecision:
        costs = (
            self._policy.fee_bps
            + spread_bps
            + self._policy.expected_slippage_bps
            + self._policy.infrastructure_bps
            + self._policy.funding_bps
            + self._policy.model_bps
            + self._policy.latency_bps
            + self._policy.adverse_selection_bps
            + self._policy.uncertainty_buffer_bps
        )
        age_seconds = Decimal(str(max(0, (as_of - intent.signal_observed_at).total_seconds())))
        decay = max(
            Decimal("0"),
            Decimal("1") - age_seconds / (Decimal(2) * intent.expected_edge_half_life_seconds),
        )
        decayed_gross = intent.expected_gross_bps * decay
        if intent.side == Side.BUY:
            price_move = max(
                Decimal("0"),
                (current_price / intent.reference_price - 1) * Decimal("10000"),
            )
        else:
            price_move = max(
                Decimal("0"),
                (1 - current_price / intent.reference_price) * Decimal("10000"),
            )
        remaining_gross = decayed_gross - price_move
        edge = remaining_gross - costs
        result = {
            "expected_net_edge_bps": edge,
            "remaining_gross_edge_bps": remaining_gross,
            "price_move_consumed_bps": price_move,
            "signal_age_seconds": age_seconds,
        }
        if state.orders_today >= self._policy.maximum_orders_per_day:
            return FrequencyDecision(False, "DAILY_ORDER_BUDGET_EXHAUSTED", **result)
        if state.last_order_at is not None:
            elapsed_minutes = (as_of - state.last_order_at).total_seconds() / 60
            if elapsed_minutes < self._policy.cooldown_minutes:
                return FrequencyDecision(False, "SYMBOL_COOLDOWN_ACTIVE", **result)
        if intent.valid_until <= as_of:
            return FrequencyDecision(False, "INTENT_EXPIRED", **result)
        if remaining_gross <= 0:
            return FrequencyDecision(False, "ALPHA_ALREADY_CONSUMED", **result)
        if edge < self._policy.minimum_net_edge_bps:
            return FrequencyDecision(False, "INSUFFICIENT_NET_EDGE", **result)
        return FrequencyDecision(True, "FREQUENCY_ALLOWED", **result)
