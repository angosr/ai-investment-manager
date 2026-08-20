from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.domain import (
    CandidateOutcome,
    CandidateOutcomeStatus,
    SignalCandidate,
)
from investment_manager.execution.models import (
    OrderType,
    Side,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import MarketTrade
from investment_manager.market.repository import market_trades
from investment_manager.persistence import candidate_outcomes, signal_candidates


@dataclass(frozen=True, slots=True)
class CandidateSettlementResult:
    settled: int = 0
    unscorable: int = 0
    pending: int = 0


def trade_at_or_before(
    engine: Engine,
    *,
    symbol: str,
    evaluation_at: datetime,
    visible_at: datetime,
) -> MarketTrade | None:
    """Read the latest point-in-time-visible trade for an outcome horizon."""

    with engine.connect() as connection:
        payload = connection.execute(
            select(market_trades.c.payload)
            .where(
                market_trades.c.symbol == symbol,
                market_trades.c.event_time <= require_utc(evaluation_at),
                market_trades.c.observed_at <= require_utc(visible_at),
            )
            .order_by(
                market_trades.c.event_time.desc(),
                market_trades.c.aggregate_trade_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
    return MarketTrade.model_validate(payload) if payload is not None else None


class SqlCandidateOutcomeStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def pending(
        self,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[SignalCandidate, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("候选结算批次必须在 1..1000")
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(signal_candidates.c.payload)
                    .outerjoin(
                        candidate_outcomes,
                        candidate_outcomes.c.candidate_id == signal_candidates.c.candidate_id,
                    )
                    .where(
                        candidate_outcomes.c.candidate_id.is_(None),
                        signal_candidates.c.valid_until <= require_utc(as_of),
                    )
                    .order_by(signal_candidates.c.valid_until, signal_candidates.c.candidate_id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        return tuple(SignalCandidate.model_validate(candidate) for candidate in rows)

    def settled_visible_for_calibration(
        self,
        *,
        training_start: datetime,
        training_end: datetime,
        published_at: datetime,
    ) -> tuple[CandidateOutcome, ...]:
        """Read only labels that existed by publication time; scope filtering is pure."""

        start = require_utc(training_start)
        end = require_utc(training_end)
        published = require_utc(published_at)
        if not start < end <= published:
            raise ValueError("校准查询时间边界非法")
        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(candidate_outcomes.c.payload)
                    .where(
                        candidate_outcomes.c.status == CandidateOutcomeStatus.SETTLED.value,
                        candidate_outcomes.c.evaluation_at >= start,
                        candidate_outcomes.c.evaluation_at <= end,
                        candidate_outcomes.c.settled_at <= published,
                    )
                    .order_by(
                        candidate_outcomes.c.evaluation_at,
                        candidate_outcomes.c.outcome_id,
                    )
                ).scalars()
            )
        return tuple(CandidateOutcome.model_validate(payload) for payload in payloads)

    def visible_path_trades(
        self,
        *,
        symbol: str,
        signal_observed_at: datetime,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> tuple[MarketTrade, ...]:
        signal_at = require_utc(signal_observed_at)
        evaluation = require_utc(evaluation_at)
        visible = min(require_utc(visible_at), evaluation)
        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(market_trades.c.payload)
                    .where(
                        market_trades.c.symbol == symbol,
                        market_trades.c.event_time >= signal_at,
                        market_trades.c.event_time <= evaluation,
                        market_trades.c.observed_at >= signal_at,
                        market_trades.c.observed_at <= visible,
                    )
                    .order_by(
                        market_trades.c.observed_at,
                        market_trades.c.aggregate_trade_id,
                    )
                ).scalars()
            )
        return tuple(MarketTrade.model_validate(payload) for payload in payloads)

    def record(self, outcome: CandidateOutcome) -> bool:
        payload = outcome.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(candidate_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        candidate_id=outcome.candidate_id,
                        cycle_id=outcome.cycle_id,
                        status=outcome.status.value,
                        evaluation_at=outcome.evaluation_at,
                        settled_at=outcome.settled_at,
                        net_return_bps=outcome.net_return_bps,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(candidate_outcomes.c.payload).where(
                        candidate_outcomes.c.candidate_id == outcome.candidate_id
                    )
                ).scalar_one_or_none()
            if existing is None or CandidateOutcome.model_validate(existing) != outcome:
                raise ValueError("CandidateOutcome 已存在且内容不同") from None
            return False


@dataclass(slots=True)
class CandidateOutcomeSettler:
    store: SqlCandidateOutcomeStore
    evaluation_version: str
    maximum_market_age_seconds: int
    settlement_grace_minutes: int
    batch_size: int = 100

    def settle(self, *, as_of: datetime) -> CandidateSettlementResult:
        as_of = require_utc(as_of)
        settled = unscorable = pending = 0
        for candidate in self.store.pending(as_of=as_of, limit=self.batch_size):
            evaluation_at = candidate.signal_observed_at + timedelta(
                minutes=candidate.horizon_minutes
            )
            if evaluation_at > as_of:
                pending += 1
                continue
            cost_basis_frozen = candidate.has_frozen_cost_basis
            execution_version = candidate.execution_policy_version or "unfrozen-legacy"
            frequency_version = candidate.frequency_policy_version or "unfrozen-legacy"
            cost_bps = candidate.estimated_cost_bps or Decimal("0")
            common = {
                "outcome_id": stable_id(
                    "candidate_outcome", candidate.candidate_id, self.evaluation_version
                ),
                "candidate_id": candidate.candidate_id,
                "cycle_id": candidate.cycle_id,
                "producer_id": candidate.producer_id,
                "producer_version": candidate.producer_version,
                "calibration_ref": candidate.calibration_ref,
                "evaluation_version": self.evaluation_version,
                "execution_policy_version": execution_version,
                "frequency_policy_version": frequency_version,
                "symbol": candidate.symbol,
                "side": candidate.side,
                "signal_observed_at": candidate.signal_observed_at,
                "evaluation_at": evaluation_at,
                "settled_at": as_of,
                "reference_price": candidate.reference_price,
                "estimated_cost_bps": cost_bps,
            }
            if not cost_basis_frozen:
                outcome = CandidateOutcome(
                    **common,
                    status=CandidateOutcomeStatus.UNSCORABLE,
                    reason_code="COST_BASIS_NOT_FROZEN",
                )
                unscorable += int(self.store.record(outcome))
                continue
            trades = self.store.visible_path_trades(
                symbol=candidate.symbol,
                signal_observed_at=candidate.signal_observed_at,
                evaluation_at=evaluation_at,
                visible_at=as_of,
            )
            entry_trade = self._entry_trade(candidate, trades)
            timely_entry = entry_trade is not None and (
                candidate.entry.order_type == OrderType.LIMIT
                or entry_trade.observed_at - candidate.signal_observed_at
                <= timedelta(seconds=self.maximum_market_age_seconds)
            )
            horizon_trade = max(
                trades,
                key=lambda item: (item.event_time, item.aggregate_trade_id),
                default=None,
            )
            fresh_horizon = horizon_trade is not None and timedelta(
                0
            ) <= evaluation_at - horizon_trade.event_time <= timedelta(
                seconds=self.maximum_market_age_seconds
            )
            if timely_entry and fresh_horizon:
                assert entry_trade is not None and horizon_trade is not None
                stop_trade = self._stop_trade(candidate, entry_trade, trades)
                exit_trade = stop_trade or horizon_trade
                gross = self._gross_return_bps(
                    candidate.side,
                    entry_trade.price,
                    exit_trade.price,
                )
                outcome = CandidateOutcome(
                    **common,
                    status=CandidateOutcomeStatus.SETTLED,
                    entry_price=entry_trade.price,
                    entry_event_time=entry_trade.event_time,
                    entry_observed_at=entry_trade.observed_at,
                    exit_price=exit_trade.price,
                    exit_event_time=exit_trade.event_time,
                    exit_observed_at=exit_trade.observed_at,
                    gross_return_bps=gross,
                    net_return_bps=gross - cost_bps,
                    reason_code=(
                        "STOP_LOSS_TRIGGERED"
                        if stop_trade is not None
                        else "HORIZON_RETURN_AVAILABLE"
                    ),
                )
                settled += int(self.store.record(outcome))
                continue
            if as_of - evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                pending += 1
                continue
            outcome = CandidateOutcome(
                **common,
                status=CandidateOutcomeStatus.UNSCORABLE,
                reason_code=(
                    "ENTRY_NOT_FILLED_OR_STALE"
                    if not timely_entry
                    else "MARKET_DATA_MISSING_AT_HORIZON"
                ),
            )
            unscorable += int(self.store.record(outcome))
        return CandidateSettlementResult(
            settled=settled,
            unscorable=unscorable,
            pending=pending,
        )

    @staticmethod
    def _entry_trade(
        candidate: SignalCandidate,
        trades: tuple[MarketTrade, ...],
    ) -> MarketTrade | None:
        for trade in trades:
            if trade.observed_at > candidate.valid_until:
                return None
            if candidate.entry.order_type == OrderType.MARKET:
                return trade
            assert candidate.entry.price is not None
            if candidate.side == Side.BUY and trade.price <= candidate.entry.price:
                return trade
            if candidate.side == Side.SELL and trade.price >= candidate.entry.price:
                return trade
        return None

    @staticmethod
    def _stop_trade(
        candidate: SignalCandidate,
        entry_trade: MarketTrade,
        trades: tuple[MarketTrade, ...],
    ) -> MarketTrade | None:
        for trade in trades:
            if trade.observed_at < entry_trade.observed_at:
                continue
            if candidate.side == Side.BUY and trade.price <= candidate.stop_price:
                return trade
            if candidate.side == Side.SELL and trade.price >= candidate.stop_price:
                return trade
        return None

    @staticmethod
    def _gross_return_bps(
        side: Side,
        entry_price: Decimal,
        exit_price: Decimal,
    ) -> Decimal:
        if side == Side.BUY:
            fraction = exit_price / entry_price - Decimal("1")
        else:
            fraction = Decimal("1") - exit_price / entry_price
        return fraction * Decimal("10000")
