from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from investment_manager.domain import (
    SignalCandidate,
    TradeIntent,
)
from investment_manager.execution.models import (
    OrderType,
    Side,
)
from investment_manager.execution.policy import ExecutionPolicy
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import MarketSnapshot
from investment_manager.portfolio.policy import CompositionPolicy, FrequencyPolicy


@dataclass(frozen=True, slots=True)
class CompositionResult:
    intent: TradeIntent | None
    reason_code: str


class HighestNetEdgeComposer:
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
            and candidate.has_frozen_cost_basis
        ]
        if not valid:
            return CompositionResult(intent=None, reason_code="NO_VALID_CANDIDATE")
        valid.sort(key=self._economic_rank)
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
                program_exit=selected.program_exit,
            ),
            reason_code="COMPOSED",
        )

    @staticmethod
    def _economic_rank(candidate: SignalCandidate) -> tuple[Decimal, str]:
        if candidate.estimated_cost_bps is None:
            raise ValueError("合成前必须冻结候选成本")
        conservative_net_edge = candidate.expected_gross_bps - candidate.estimated_cost_bps
        return -conservative_net_edge, candidate.candidate_id


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
    def __init__(self, policy: FrequencyPolicy, execution: ExecutionPolicy) -> None:
        self._policy = policy
        self._execution = execution

    def evaluate(
        self,
        intent: TradeIntent,
        *,
        as_of: datetime,
        spread_bps: Decimal,
        current_price: Decimal,
        state: FrequencyState,
    ) -> FrequencyDecision:
        costs = estimate_round_trip_cost_bps(
            entry_order_type=intent.entry.order_type,
            spread_bps=spread_bps,
            frequency=self._policy,
            execution=self._execution,
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


def estimate_round_trip_cost_bps(
    *,
    entry_order_type: OrderType,
    spread_bps: Decimal,
    frequency: FrequencyPolicy,
    execution: ExecutionPolicy,
) -> Decimal:
    """冻结一次完整建仓和平仓的可归因交易成本，供门禁和反事实评价共用。"""

    fee_cost = Decimal("2") * execution.fee_bps
    market_slippage_legs = Decimal("2" if entry_order_type == OrderType.MARKET else "1")
    return (
        fee_cost
        + market_slippage_legs * execution.market_slippage_bps
        + spread_bps
        + frequency.funding_bps
        + frequency.latency_bps
        + frequency.adverse_selection_bps
        + frequency.uncertainty_buffer_bps
    )


def estimate_round_trip_cost_amount(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    entry_order_type: OrderType,
    spread_bps: Decimal,
    frequency: FrequencyPolicy,
    execution: ExecutionPolicy,
) -> Decimal:
    """Model realized round-trip costs on each leg's own notional."""

    if entry_price <= 0 or exit_price <= 0 or quantity <= 0 or spread_bps < 0:
        raise ValueError("往返成本的价格、数量必须为正，价差不能为负")
    entry_notional = entry_price * quantity
    exit_notional = exit_price * quantity
    fee = (entry_notional + exit_notional) * execution.fee_bps
    entry_slippage = (
        entry_notional * execution.market_slippage_bps
        if entry_order_type == OrderType.MARKET
        else Decimal("0")
    )
    exit_slippage = exit_notional * execution.market_slippage_bps
    # A symmetric quote contributes half the full spread on each crossing.
    spread = (entry_notional + exit_notional) * spread_bps / Decimal("2")
    other_bps = (
        frequency.funding_bps
        + frequency.latency_bps
        + frequency.adverse_selection_bps
        + frequency.uncertainty_buffer_bps
    )
    other = entry_notional * other_bps
    return (fee + entry_slippage + exit_slippage + spread + other) / Decimal(
        "10000"
    )


def freeze_candidate_cost_basis(
    candidate: SignalCandidate,
    *,
    market: MarketSnapshot,
    frequency: FrequencyPolicy,
    execution: ExecutionPolicy,
) -> SignalCandidate:
    """在候选产生周期冻结完整成本口径，避免结算时配置漂移污染标签。"""

    if candidate.has_frozen_cost_basis:
        raise ValueError("候选生产者不得预填系统成本依据")
    spread_bps = (market.ask - market.bid) / market.last * Decimal("10000")
    return candidate.model_copy(
        update={
            "execution_policy_version": execution.version,
            "frequency_policy_version": frequency.version,
            "estimated_cost_bps": estimate_round_trip_cost_bps(
                entry_order_type=candidate.entry.order_type,
                spread_bps=spread_bps,
                frequency=frequency,
                execution=execution,
            ),
        }
    )
