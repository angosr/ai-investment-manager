from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, func, select

from investment_manager.cycle import AnalysisCycle
from investment_manager.execution.ledger import InMemoryFactLedger
from investment_manager.execution.legacy_exchange import MockExchange
from investment_manager.execution.lifecycle import PositionLifecycleManager
from investment_manager.execution.mock_repository import SqlMockExchange
from investment_manager.execution.models import (
    Order,
    OrderStatus,
)
from investment_manager.execution.reconciliation import MockReconciler
from investment_manager.execution.tables import (
    execution_requests,
    mock_exchange_orders,
    orders,
    position_lifecycles,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.legacy.models import CycleOutcome
from investment_manager.market.models import MarketSnapshot
from investment_manager.persistence import (
    SqlFactLedger,
    SqlLifecycleLedger,
    decision_outcomes,
)
from investment_manager.risk.budget import (
    SqlRiskBudgetStore,
    portfolio_risk_budgets,
    risk_reservations,
)
from investment_manager.schema import create_schema


class RejectingExchange(MockExchange):
    def submit(self, *, intent, risk, market) -> Order:
        client_order_id = stable_id("mock-reject", intent.intent_id)[:36]
        return Order(
            order_id=stable_id("order", client_order_id),
            client_order_id=client_order_id,
            cycle_id=intent.cycle_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.entry.order_type,
            requested_quantity=risk.quantity,
            limit_price=intent.entry.price,
            status=OrderStatus.REJECTED,
        )


def test_prepare_atomically_reserves_then_execution_consumes(app_config, replay_input) -> None:
    ledger = InMemoryFactLedger()
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=ledger.risk_budget,
    )

    prepared = cycle.prepare(replay_input)

    assert prepared.outcome == CycleOutcome.EXECUTION_PENDING
    assert prepared.execution_request is not None
    reservation = prepared.execution_request.risk_decision.reservation
    assert reservation is not None
    assert ledger.risk_budget.status(reservation.reservation_id) == "ACTIVE"
    assert prepared.order is None

    completed = cycle.execute(prepared.execution_request)
    replayed = cycle.execute(prepared.execution_request)

    assert completed.outcome == CycleOutcome.EXECUTED
    assert replayed == completed
    assert ledger.risk_budget.status(reservation.reservation_id) == "CONSUMED"
    assert len(cycle.exchange.orders) == 1


def test_terminal_rejection_releases_reserved_risk(app_config, replay_input) -> None:
    ledger = InMemoryFactLedger()
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=RejectingExchange(app_config.execution),
        risk_budget=ledger.risk_budget,
    )
    prepared = cycle.prepare(replay_input)
    assert prepared.execution_request is not None
    reservation = prepared.execution_request.risk_decision.reservation
    assert reservation is not None

    completed = cycle.execute(prepared.execution_request)

    assert completed.outcome == CycleOutcome.NO_TRADE
    assert completed.order is not None
    assert completed.order.status == OrderStatus.REJECTED
    assert ledger.risk_budget.status(reservation.reservation_id) == "RELEASED"


def test_expired_execution_never_submits_and_releases_reserved_risk(
    app_config, replay_input
) -> None:
    ledger = InMemoryFactLedger()
    exchange = MockExchange(app_config.execution)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=exchange,
        risk_budget=ledger.risk_budget,
    )
    prepared = cycle.prepare(replay_input)
    request = prepared.execution_request
    assert request is not None and request.risk_decision.reservation is not None

    completed = cycle.execute(
        request,
        observed_at=request.risk_decision.reservation.expires_at,
    )

    assert completed.outcome == CycleOutcome.NO_TRADE
    assert completed.reason_code == "EXECUTION_SIGNAL_EXPIRED"
    assert completed.order is not None and completed.order.status == OrderStatus.EXPIRED
    assert exchange.orders == ()
    assert ledger.risk_budget.status(request.risk_decision.reservation.reservation_id) == "RELEASED"
    handoff = next(
        item
        for item in completed.metrics
        if dict(item.dimensions).get("metric") == "execution_handoff_age_seconds"
    )
    assert handoff.value > 0


def test_sql_pending_request_recovers_after_process_restart(app_config, replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    first = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    prepared = first.prepare(replay_input)
    assert prepared.execution_request is not None
    first.exchange.submit(
        intent=prepared.execution_request.intent,
        risk=prepared.execution_request.risk_decision,
        market=prepared.execution_request.market,
    )

    with engine.connect() as connection:
        request = connection.execute(select(execution_requests)).mappings().one()
        reservation = connection.execute(select(risk_reservations)).mappings().one()
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
    assert request["status"] == "PENDING"
    assert reservation["status"] == "ACTIVE"
    assert budget["reserved_amount"] > 0
    assert budget["exposure_risk_amount"] == 0

    restarted = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    recovered = restarted.prepare(replay_input)
    assert recovered == prepared
    completed = restarted.execute(recovered.execution_request)

    assert completed.outcome == CycleOutcome.EXECUTED
    with engine.connect() as connection:
        request = connection.execute(select(execution_requests)).mappings().one()
        reservation = connection.execute(select(risk_reservations)).mappings().one()
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
        order_count = connection.scalar(select(func.count()).select_from(orders))
        remote_order_count = connection.scalar(
            select(func.count()).select_from(mock_exchange_orders)
        )
    assert request["status"] == "COMPLETED"
    assert request["result_payload"] is not None
    assert reservation["status"] == "CONSUMED"
    assert budget["reserved_amount"] == 0
    assert budget["exposure_risk_amount"] > 0
    assert order_count == 1
    assert remote_order_count == 1


def test_budget_rejection_commits_terminal_decision_without_request(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    strict_risk = app_config.risk.model_copy(
        update={"maximum_total_risk_fraction": Decimal("0.000001")}
    )
    strict_config = app_config.model_copy(update={"risk": strict_risk})
    cycle = AnalysisCycle.with_adapters(
        strict_config,
        ledger=SqlFactLedger(engine),
        exchange=MockExchange(strict_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )

    result = cycle.run(replay_input)
    replayed = cycle.run(replay_input)

    assert result.outcome == CycleOutcome.NO_TRADE
    assert result.reason_code == "PORTFOLIO_RISK_BUDGET_EXHAUSTED"
    assert result.execution_request is None
    assert replayed == result
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(execution_requests)) == 0
        assert connection.scalar(select(func.count()).select_from(risk_reservations)) == 0


def test_lifecycle_close_rolls_back_risk_release_when_fact_commit_crashes(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    opened = cycle.run(replay_input)
    assert opened.position_lifecycle is not None
    assert opened.account_after is not None
    close_time = opened.position_lifecycle.max_exit_at + timedelta(minutes=1)
    close_market = MarketSnapshot.model_validate(
        {
            **replay_input.market.model_dump(mode="json"),
            "cycle_id": "lifecycle-atomic-close",
            "as_of": close_time,
            "observed_at": close_time,
        }
    )
    lifecycle_ledger = SqlLifecycleLedger(engine)
    manager = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
        lifecycle_ledger=lifecycle_ledger,
    )

    def crash_before_outcome(_connection, clauseelement, _multiparams, _params, _execution_options):
        if getattr(clauseelement, "table", None) is decision_outcomes:
            raise RuntimeError("simulated lifecycle commit crash")

    event.listen(engine, "before_execute", crash_before_outcome)
    with pytest.raises(RuntimeError, match="lifecycle commit crash"):
        manager.evaluate(
            lifecycle=opened.position_lifecycle,
            market=close_market,
            account=opened.account_after,
            pipeline_version=app_config.pipeline.version,
        )
    event.remove(engine, "before_execute", crash_before_outcome)

    with engine.connect() as connection:
        reservation = connection.execute(select(risk_reservations)).mappings().one()
        lifecycle = connection.execute(select(position_lifecycles)).mappings().one()
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
        outcome_count = connection.scalar(select(func.count()).select_from(decision_outcomes))
    assert reservation["status"] == "CONSUMED"
    assert lifecycle["status"] == "PROTECTED"
    assert budget["exposure_risk_amount"] > 0
    assert outcome_count == 0

    closed = manager.evaluate(
        lifecycle=opened.position_lifecycle,
        market=close_market,
        account=opened.account_after,
        pipeline_version=app_config.pipeline.version,
    )
    assert closed.outcome is not None
    with engine.connect() as connection:
        reservation = connection.execute(select(risk_reservations)).mappings().one()
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
    assert reservation["status"] == "RELEASED"
    assert budget["exposure_risk_amount"] == 0
