from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from temporalio.testing import WorkflowEnvironment

from quant_core.cycle import AnalysisCycle
from quant_core.temporal_runtime import AnalysisTemporalWorker, TemporalAnalysisCoordinator
from quant_core.trigger import TriggerDecision, TriggerReason
from quant_core.workflow import (
    WorkflowExecutionStatus,
    WorkflowRequest,
    build_workflow_request,
)


def _request(app_config, replay_input, *, deadline_delta=timedelta(hours=1)):
    created_at = datetime.now(UTC)
    return build_workflow_request(
        cycle_input=replay_input,
        trigger=TriggerDecision(should_run=True, reason=TriggerReason.HEARTBEAT),
        temporal_policy=app_config.temporal,
        created_at=created_at,
        deadline=created_at + deadline_delta,
    )


def test_request_identity_covers_orchestration_policy(app_config, replay_input) -> None:
    request = _request(app_config, replay_input)
    raw = request.model_dump(mode="json")
    raw["orchestration"]["retry_maximum_attempts"] += 1

    try:
        WorkflowRequest.model_validate(raw)
    except ValueError as exc:
        assert "input_hash" in str(exc)
    else:
        raise AssertionError("篡改后的冻结输入必须被拒绝")


def test_temporal_replay_and_deadline(app_config, replay_input) -> None:
    async def scenario() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"task_queue": "quant-core-analysis-test"}
            )
            cycle = AnalysisCycle.create(app_config)
            coordinator = TemporalAnalysisCoordinator(env.client, policy)
            request = _request(app_config, replay_input)

            async with AnalysisTemporalWorker(env.client, policy, cycle):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)
                conflicting = _request(
                    app_config,
                    replay_input.model_copy(update={"frequency_orders_today": 1}),
                )
                try:
                    await coordinator.execute(conflicting)
                except ValueError as exc:
                    assert "冻结输入不同" in str(exc)
                else:
                    raise AssertionError("相同 cycle_id 的不同冻结输入必须被拒绝")

            assert first.status == WorkflowExecutionStatus.COMPLETED
            assert replayed == first
            assert len(cycle.exchange.orders) == 1

            expired = _request(
                app_config,
                replay_input.model_copy(
                    update={
                        "market": replay_input.market.model_copy(
                            update={"cycle_id": "expired-cycle"}
                        ),
                        "account": replay_input.account.model_copy(
                            update={"cycle_id": "expired-cycle"}
                        ),
                    }
                ),
                deadline_delta=timedelta(seconds=0),
            )
            async with AnalysisTemporalWorker(env.client, policy, cycle):
                no_trade = await coordinator.execute(expired)
            assert no_trade.status == WorkflowExecutionStatus.NO_TRADE
            assert no_trade.reason_code == "ANALYSIS_DEADLINE_EXPIRED"

    asyncio.run(scenario())


def test_temporal_activity_retries_transient_failure(app_config, replay_input) -> None:
    @dataclass
    class FailOnceCycle:
        delegate: AnalysisCycle
        calls: int = 0

        def prepare(self, cycle_input):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return self.delegate.prepare(cycle_input)

        def execute(self, request):
            return self.delegate.execute(request)

    async def scenario() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"task_queue": "quant-core-analysis-retry-test"}
            )
            retry_input = replay_input.model_copy(
                update={
                    "market": replay_input.market.model_copy(update={"cycle_id": "retry-cycle"}),
                    "account": replay_input.account.model_copy(update={"cycle_id": "retry-cycle"}),
                }
            )
            request = _request(app_config, retry_input, deadline_delta=timedelta(days=1))
            flaky = FailOnceCycle(AnalysisCycle.create(app_config))
            coordinator = TemporalAnalysisCoordinator(env.client, policy)
            async with AnalysisTemporalWorker(env.client, policy, flaky):  # type: ignore[arg-type]
                retried = await coordinator.execute(request)
            assert retried.status == WorkflowExecutionStatus.COMPLETED, retried
            assert retried.attempt == 2
            assert flaky.calls == 2

    asyncio.run(scenario())


def test_execution_activity_recovers_after_submit_before_commit_crash(
    app_config, replay_input
) -> None:
    @dataclass
    class CrashAfterSubmitCycle:
        delegate: AnalysisCycle
        execution_calls: int = 0

        def prepare(self, cycle_input):
            return self.delegate.prepare(cycle_input)

        def execute(self, request):
            self.execution_calls += 1
            if self.execution_calls == 1:
                self.delegate.exchange.submit(
                    intent=request.intent,
                    risk=request.risk_decision,
                    market=request.market,
                )
                raise RuntimeError("simulated crash after exchange response")
            return self.delegate.execute(request)

    async def scenario() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"task_queue": "quant-core-execution-crash-test"}
            )
            cycle_input = replay_input.model_copy(
                update={
                    "market": replay_input.market.model_copy(
                        update={"cycle_id": "execution-crash-cycle"}
                    ),
                    "account": replay_input.account.model_copy(
                        update={"cycle_id": "execution-crash-cycle"}
                    ),
                }
            )
            request = _request(app_config, cycle_input, deadline_delta=timedelta(days=1))
            delegate = AnalysisCycle.create(app_config)
            crashing = CrashAfterSubmitCycle(delegate)
            coordinator = TemporalAnalysisCoordinator(env.client, policy)

            async with AnalysisTemporalWorker(env.client, policy, crashing):  # type: ignore[arg-type]
                result = await coordinator.execute(request)

            assert result.status == WorkflowExecutionStatus.COMPLETED
            assert result.attempt == 2
            assert crashing.execution_calls == 2
            assert len(delegate.exchange.orders) == 1
            assert result.cycle_result is not None
            assert result.cycle_result.execution_request is not None
            reservation = result.cycle_result.execution_request.risk_decision.reservation
            assert reservation is not None
            assert delegate.risk_budget.status(reservation.reservation_id) == "CONSUMED"

    asyncio.run(scenario())
