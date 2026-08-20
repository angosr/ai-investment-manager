from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select, update

from quant_core.candidate_evaluation import CandidateOutcomeSettler, SqlCandidateOutcomeStore
from quant_core.cycle import AnalysisCycle
from quant_core.domain import CandidateOutcome, CandidateOutcomeStatus
from quant_core.market_data import MarketTrade
from quant_core.market_data_sql import SqlMarketDataStore, create_market_schema
from quant_core.mock_exchange_sql import SqlMockExchange
from quant_core.persistence import (
    SqlFactLedger,
    candidate_outcomes,
    signal_candidates,
)
from quant_core.risk_budget import SqlRiskBudgetStore
from quant_core.schema import create_schema


def _seed_candidate(app_config, replay_input):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    create_market_schema(engine)
    result = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    ).run(replay_input)
    facts = SqlFactLedger(engine).get(result.cycle_id)
    assert facts is not None and facts.candidates
    return engine, facts.candidates[0]


def _settler(engine, app_config) -> CandidateOutcomeSettler:
    return CandidateOutcomeSettler(
        store=SqlCandidateOutcomeStore(engine),
        evaluation_version=app_config.outcome_evaluation.version,
        maximum_market_age_seconds=app_config.risk.maximum_market_age_seconds,
        settlement_grace_minutes=app_config.outcome_evaluation.settlement_grace_minutes,
    )


def _stored_outcome(engine) -> CandidateOutcome:
    with engine.connect() as connection:
        payload = connection.execute(select(candidate_outcomes.c.payload)).scalar_one()
    return CandidateOutcome.model_validate(payload)


def _put_trade(
    engine,
    candidate,
    *,
    trade_id: int,
    at,
    price: Decimal,
) -> None:
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id=f"candidate-trade-{trade_id}",
            symbol=candidate.symbol,
            aggregate_trade_id=trade_id,
            event_time=at,
            observed_at=at,
            price=price,
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="test",
        )
    )


def test_candidate_outcome_scores_rejected_or_executed_signal_without_touching_pnl(
    app_config, replay_input
) -> None:
    engine, candidate = _seed_candidate(app_config, replay_input)
    evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)
    exit_price = candidate.reference_price * Decimal("1.01")
    entry_at = candidate.signal_observed_at + timedelta(seconds=1)
    _put_trade(
        engine,
        candidate,
        trade_id=9_000_000_000,
        at=entry_at,
        price=candidate.reference_price,
    )
    _put_trade(
        engine,
        candidate,
        trade_id=9_000_000_001,
        at=evaluation_at,
        price=exit_price,
    )

    first = _settler(engine, app_config).settle(as_of=evaluation_at + timedelta(seconds=1))
    replayed = _settler(engine, app_config).settle(as_of=evaluation_at + timedelta(seconds=2))
    outcome = _stored_outcome(engine)

    assert first.settled == 1
    assert replayed.settled == 0
    assert outcome.status == CandidateOutcomeStatus.SETTLED
    assert outcome.gross_return_bps == Decimal("100")
    assert outcome.entry_price == candidate.reference_price
    assert outcome.entry_event_time == entry_at
    assert outcome.entry_observed_at == entry_at
    assert outcome.exit_observed_at == evaluation_at
    assert outcome.estimated_cost_bps == candidate.estimated_cost_bps
    assert outcome.execution_policy_version == candidate.execution_policy_version
    assert outcome.frequency_policy_version == candidate.frequency_policy_version
    assert outcome.net_return_bps == outcome.gross_return_bps - outcome.estimated_cost_bps

    store = SqlCandidateOutcomeStore(engine)
    assert (
        store.settled_visible_for_calibration(
            training_start=candidate.signal_observed_at - timedelta(seconds=1),
            training_end=evaluation_at,
            published_at=evaluation_at,
        )
        == ()
    )
    assert store.settled_visible_for_calibration(
        training_start=candidate.signal_observed_at - timedelta(seconds=1),
        training_end=evaluation_at,
        published_at=evaluation_at + timedelta(seconds=1),
    ) == (outcome,)


def test_candidate_outcome_marks_missing_horizon_market_unscorable(
    app_config, replay_input
) -> None:
    engine, candidate = _seed_candidate(app_config, replay_input)
    evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)
    _put_trade(
        engine,
        candidate,
        trade_id=9_000_000_000,
        at=candidate.signal_observed_at + timedelta(seconds=1),
        price=candidate.reference_price,
    )

    result = _settler(engine, app_config).settle(
        as_of=evaluation_at
        + timedelta(minutes=app_config.outcome_evaluation.settlement_grace_minutes, seconds=1)
    )
    outcome = _stored_outcome(engine)

    assert result.unscorable == 1
    assert outcome.status == CandidateOutcomeStatus.UNSCORABLE
    assert outcome.exit_price is None
    assert outcome.net_return_bps is None
    assert outcome.reason_code == "MARKET_DATA_MISSING_AT_HORIZON"


def test_candidate_outcome_waits_for_late_market_data_during_settlement_grace(
    app_config, replay_input
) -> None:
    engine, candidate = _seed_candidate(app_config, replay_input)
    evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)

    result = _settler(engine, app_config).settle(
        as_of=evaluation_at
        + timedelta(minutes=app_config.outcome_evaluation.settlement_grace_minutes - 1)
    )

    assert result.pending == 1
    with engine.connect() as connection:
        assert connection.execute(select(candidate_outcomes.c.outcome_id)).first() is None


def test_candidate_without_frozen_cost_basis_is_unscorable(app_config, replay_input) -> None:
    engine, candidate = _seed_candidate(app_config, replay_input)
    legacy = candidate.model_copy(
        update={
            "execution_policy_version": None,
            "frequency_policy_version": None,
            "estimated_cost_bps": None,
        }
    )
    with engine.begin() as connection:
        connection.execute(
            update(signal_candidates)
            .where(signal_candidates.c.candidate_id == candidate.candidate_id)
            .values(payload=legacy.model_dump(mode="json"))
        )
    evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)

    result = _settler(engine, app_config).settle(as_of=evaluation_at)
    outcome = _stored_outcome(engine)

    assert result.unscorable == 1
    assert outcome.status == CandidateOutcomeStatus.UNSCORABLE
    assert outcome.reason_code == "COST_BASIS_NOT_FROZEN"
    assert outcome.execution_policy_version == "unfrozen-legacy"
    assert outcome.frequency_policy_version == "unfrozen-legacy"


def test_candidate_outcome_uses_first_visible_entry_and_stop_before_horizon(
    app_config, replay_input
) -> None:
    engine, candidate = _seed_candidate(app_config, replay_input)
    entry_at = candidate.signal_observed_at + timedelta(seconds=1)
    stop_at = entry_at + timedelta(minutes=5)
    evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)
    entry_price = candidate.reference_price * Decimal("1.001")
    stop_fill = candidate.stop_price * Decimal("0.999")
    _put_trade(
        engine,
        candidate,
        trade_id=9_000_000_000,
        at=entry_at,
        price=entry_price,
    )
    _put_trade(
        engine,
        candidate,
        trade_id=9_000_000_001,
        at=stop_at,
        price=stop_fill,
    )
    _put_trade(
        engine,
        candidate,
        trade_id=9_000_000_002,
        at=evaluation_at,
        price=candidate.reference_price * Decimal("1.02"),
    )

    result = _settler(engine, app_config).settle(as_of=evaluation_at + timedelta(seconds=1))
    outcome = _stored_outcome(engine)

    assert result.settled == 1
    assert outcome.reason_code == "STOP_LOSS_TRIGGERED"
    assert outcome.entry_price == entry_price
    assert outcome.exit_price == stop_fill
    assert outcome.exit_event_time == stop_at
    assert outcome.gross_return_bps == (stop_fill / entry_price - Decimal("1")) * Decimal(
        "10000"
    )
