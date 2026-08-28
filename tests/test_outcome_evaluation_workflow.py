from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from investment_manager.governance.evaluation.outcome_service import (
    OutcomeEvaluationSupervisor,
    _seconds_until_next_poll,
    assemble_outcome_evaluation,
)


def test_outcome_evaluation_poll_uses_absolute_utc_buckets() -> None:
    almost_boundary = datetime(2026, 8, 18, 12, 4, 59, 750000, tzinfo=UTC)
    after_slow_run = datetime(2026, 8, 18, 12, 5, 7, tzinfo=UTC)

    assert _seconds_until_next_poll(almost_boundary, poll_seconds=300) == 0.25
    assert _seconds_until_next_poll(after_slow_run, poll_seconds=300) == 293


def test_outcome_service_keeps_settling_recorded_product_obligations(app_config) -> None:
    context = app_config.capital.context_forecast
    assert context is not None
    config = app_config.model_copy(
        update={
            "capital": app_config.capital.model_copy(
                update={"context_forecast": context.model_copy(update={"product_payoffs": None})}
            ),
        }
    )

    supervisor = assemble_outcome_evaluation(config, "sqlite+pysqlite:///:memory:")

    assert supervisor.product_payoff_settler is not None


def test_outcome_supervisor_settles_current_release_cohorts(app_config) -> None:
    class Settler:
        def __init__(self, result, *, stop: asyncio.Event | None = None) -> None:
            self.result = result
            self.stop = stop
            self.calls = []

        def settle(self, *, as_of):
            self.calls.append(as_of)
            if self.stop is not None:
                self.stop.set()
            return self.result

    async def scenario() -> None:
        stop = asyncio.Event()
        target_forecast = Settler(SimpleNamespace(settled=9, outcome_unavailable=10, pending=11))
        product_payoff = Settler(
            SimpleNamespace(settled=12, outcome_unavailable=13, pending=14),
            stop=stop,
        )
        supervisor = OutcomeEvaluationSupervisor(
            config=app_config,
            target_forecast_settler=target_forecast,
            product_payoff_settler=product_payoff,
            clock=lambda: datetime(2026, 8, 20, 12, tzinfo=UTC),
        )

        await supervisor.run(stop)

        assert len(target_forecast.calls) == len(product_payoff.calls) == 1
        assert supervisor.health.target_forecast_settled == 9
        assert supervisor.health.target_forecast_outcome_unavailable == 10
        assert supervisor.health.target_forecast_pending == 11
        assert supervisor.health.product_payoff_settled == 12
        assert supervisor.health.product_payoff_outcome_unavailable == 13
        assert supervisor.health.product_payoff_pending == 14

    asyncio.run(scenario())


def test_quant_product_projection_uses_independent_research_poll(app_config) -> None:
    class Settler:
        def settle(self, *, as_of):
            return SimpleNamespace(settled=0, outcome_unavailable=0, pending=0)

    class ProductProjectionRecorder:
        def __init__(self, stop: asyncio.Event) -> None:
            self.stop = stop
            self.calls = 0

        def reconcile(self, *, as_of):
            self.calls += 1
            if self.calls == 2:
                self.stop.set()
            return SimpleNamespace(projection_count=4, unavailable_forecast_count=1)

    async def scenario() -> None:
        stop = asyncio.Event()
        recorder = ProductProjectionRecorder(stop)
        config = app_config.model_copy(
            update={
                "outcome_evaluation": app_config.outcome_evaluation.model_copy(
                    update={"research_poll_seconds": 1}
                )
            }
        )
        supervisor = OutcomeEvaluationSupervisor(
            config=config,
            target_forecast_settler=Settler(),
            research_product_projection_recorder=recorder,
            clock=lambda: datetime.now(UTC),
        )

        await asyncio.wait_for(supervisor.run(stop), timeout=2)

        assert recorder.calls == 2
        assert supervisor.health.research_product_projection_count == 8
        assert supervisor.health.research_product_projection_unavailable == 1

    asyncio.run(scenario())
