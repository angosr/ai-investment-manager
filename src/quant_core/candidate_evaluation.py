from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from quant_core.domain import (
    CandidateOutcome,
    CandidateOutcomeStatus,
    Side,
    SignalCandidate,
    _require_utc,
)
from quant_core.ids import stable_id
from quant_core.market_data import MarketTrade
from quant_core.market_data_sql import market_trades
from quant_core.persistence import candidate_outcomes, signal_candidates


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
                market_trades.c.event_time <= _require_utc(evaluation_at),
                market_trades.c.observed_at <= _require_utc(visible_at),
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
                        signal_candidates.c.valid_until <= _require_utc(as_of),
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

        start = _require_utc(training_start)
        end = _require_utc(training_end)
        published = _require_utc(published_at)
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

    def trade_at_or_before(
        self,
        *,
        symbol: str,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> MarketTrade | None:
        return trade_at_or_before(
            self._engine,
            symbol=symbol,
            evaluation_at=evaluation_at,
            visible_at=visible_at,
        )

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
        as_of = _require_utc(as_of)
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
            trade = self.store.trade_at_or_before(
                symbol=candidate.symbol,
                evaluation_at=evaluation_at,
                visible_at=as_of,
            )
            fresh_trade = trade is not None and timedelta(
                0
            ) <= evaluation_at - trade.event_time <= timedelta(
                seconds=self.maximum_market_age_seconds
            )
            if fresh_trade:
                assert trade is not None
                gross = self._gross_return_bps(candidate, trade.price)
                outcome = CandidateOutcome(
                    **common,
                    status=CandidateOutcomeStatus.SETTLED,
                    exit_price=trade.price,
                    exit_event_time=trade.event_time,
                    gross_return_bps=gross,
                    net_return_bps=gross - cost_bps,
                    reason_code="HORIZON_RETURN_AVAILABLE",
                )
                settled += int(self.store.record(outcome))
                continue
            if as_of - evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                pending += 1
                continue
            outcome = CandidateOutcome(
                **common,
                status=CandidateOutcomeStatus.UNSCORABLE,
                reason_code="MARKET_DATA_MISSING_AT_HORIZON",
            )
            unscorable += int(self.store.record(outcome))
        return CandidateSettlementResult(
            settled=settled,
            unscorable=unscorable,
            pending=pending,
        )

    @staticmethod
    def _gross_return_bps(candidate: SignalCandidate, exit_price: Decimal) -> Decimal:
        if candidate.side == Side.BUY:
            fraction = exit_price / candidate.reference_price - Decimal("1")
        else:
            fraction = Decimal("1") - exit_price / candidate.reference_price
        return fraction * Decimal("10000")
