import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.application import (
    AssessmentApplication,
    AssessmentCommand,
    AssessmentWorkflowStatus,
)
from investment_manager.forecast.context.contract import AssessStructuredOutput
from investment_manager.forecast.context.executor import (
    AssessmentExecutionStatus,
    ContextAssessmentExecutor,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.service import (
    WORLD_MODEL_REVIEW_MARKER,
    AssessmentTemporalCoordinator,
    AssessmentTemporalWorker,
    WorldModelReviewScheduler,
)
from investment_manager.forecast.context.settlement import (
    AssessmentViewOutcome,
    AssessmentViewOutcomeSettler,
    SqlAssessmentViewOutcomeStore,
)
from investment_manager.forecast.context.workflow import AssessmentWorkflowRequest
from investment_manager.forecast.models import (
    AssessmentUncertainty,
    ContextAssessment,
    ContextAssessmentSchemaVersion,
    ContextCausalNode,
    ContextMechanism,
    ContextMechanismRelationship,
    ContextTransmissionStage,
    ContextVerificationPredicate,
    ContextVerificationTest,
    ContextView,
    DirectionalView,
    PricedState,
)
from investment_manager.forecast.tables import assessment_executions, assessment_view_outcomes
from investment_manager.market.models import MarketTrade
from investment_manager.market.repository import SqlMarketDataStore, create_market_schema
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.models import build_initial_trigger_plan
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.schema import create_schema
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDelta,
    PacketPortfolioState,
    RequiredView,
)

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
HASH = "a" * 64


def _packet() -> DecisionPacket:
    return DecisionPacket.create(
        schema_version="packet-v1",
        policy_version="packet-policy-v1",
        mandate_version="mandate-v1",
        analysis_scope="crypto-portfolio",
        as_of=NOW,
        state_id="state-1",
        question="Assess the portfolio context.",
        trigger_ids=("delta-1",),
        required_views=(RequiredView(asset="BTC", horizon_minutes=240),),
        portfolio=PacketPortfolioState(
            quote_balance=Decimal("10000"),
            equity=Decimal("10000"),
            daily_pnl=Decimal("0"),
            drawdown_fraction=Decimal("0"),
            open_order_count=0,
            kill_switch_active=False,
            reconciled=True,
            positions=(),
        ),
        asset_states=(
            PacketAssetState(
                asset="BTC",
                market_symbol="BTCUSDT",
                observed_at=NOW,
                bid=Decimal("69999"),
                ask=Decimal("70001"),
                last=Decimal("70000"),
                return_fraction=Decimal("0.01"),
                realized_volatility=Decimal("0.3"),
                atr=Decimal("1000"),
                spread_bps=Decimal("0.3"),
                volume_ratio=Decimal("1.2"),
                regime="UPTREND",
                market_age_seconds=0,
            ),
        ),
        deltas=(
            PacketDelta(
                delta_id="delta-1",
                policy_version="delta-v1",
                category="FIRST_PARTY_FACT",
                materiality="HIGH",
                observed_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                affected_assets=("BTC",),
                risk_factors=("US_MONETARY_POLICY",),
                horizons_minutes=(240,),
                fact_revision_ids=("fact-revision-1",),
                feature_snapshot_refs=(),
                reason_codes=("FOMC_REVISION",),
            ),
        ),
        facts=(),
        active_hypotheses=(),
        previous_assessment_refs=(),
        data_quality_codes=(),
        coverage_gap_codes=(),
        missing_fact_revision_ids=(),
        omitted_fact_revision_ids=(),
    )


def _assessment() -> ContextAssessment:
    return ContextAssessment(
        assessment_id="assessment-1",
        analysis_scope="crypto-portfolio",
        mandate_version="mandate-v1",
        as_of=NOW,
        available_at=NOW + timedelta(seconds=20),
        analysis_behavior_hash="b" * 64,
        decision_packet_hash=_packet().content_hash,
        trigger_ids=("delta-1",),
        market_mechanism="A policy revision changes the discount-rate path.",
        views=(
            ContextView(
                asset="BTC",
                horizon_minutes=240,
                direction=DirectionalView.UP,
                already_priced=PricedState.PARTIAL,
                uncertainty=AssessmentUncertainty.MEDIUM,
                evidence_ids=("delta-1",),
                invalidation_conditions=("policy-reversal",),
            ),
        ),
    )


def _reference_trade(**updates) -> MarketTrade:
    values = {
        "trade_id": "trade-reference-1",
        "symbol": "BTCUSDT",
        "aggregate_trade_id": 1,
        "event_time": NOW + timedelta(seconds=19),
        "observed_at": NOW + timedelta(seconds=20),
        "price": Decimal("70010"),
        "quantity": Decimal("1"),
        "buyer_is_maker": False,
        "source": "binance",
    }
    values.update(updates)
    return MarketTrade(**values)


def _world_output_payload(claim: str) -> dict:
    return {
        "assessment": {
            "hypotheses": [
                {
                    "continuity_ref": None,
                    "role": "PRIMARY",
                    "claim": claim,
                    "horizon_hours": 24,
                    "causal_chain": [
                        {
                            "statement": "政策日程发生变化。",
                            "evidence_ids": ["delta-1"],
                        },
                        {
                            "statement": "该变化可能改变风险溢价。",
                            "evidence_ids": ["fact-revision-1"],
                        },
                    ],
                    "conflicting_evidence_ids": [],
                    "next_observation": "观察利率与风险资产响应。",
                    "invalidation_conditions": ["政策变化被正式撤回"],
                    "next_review_at": (NOW + timedelta(hours=1)).isoformat(),
                }
            ],
            "capital_implication": None,
            "decision_blockers": [],
            "event_relevance_updates": [],
        }
    }


def test_assessment_output_boundary_rejects_duplicate_evidence_items() -> None:
    payload = _world_output_payload("政策变化正在通过风险溢价影响资产定价。")
    payload["assessment"]["hypotheses"][0]["causal_chain"][0]["evidence_ids"] = [
        "delta-1",
        "delta-1",
    ]

    with pytest.raises(ValidationError, match="不能重复引用证据"):
        AssessStructuredOutput.model_validate(payload)


def test_assessment_output_requires_exactly_one_primary_hypothesis() -> None:
    payload = _world_output_payload("当前最可能的解释。")
    duplicate = dict(payload["assessment"]["hypotheses"][0])
    duplicate["claim"] = "另一个主解释。"
    payload["assessment"]["hypotheses"].append(duplicate)

    with pytest.raises(ValidationError, match="只能包含一个 PRIMARY"):
        AssessStructuredOutput.model_validate(payload)


@pytest.mark.parametrize(
    "text",
    (
        "The accepted evidence supports one causal explanation.",
        "政策变化正在改变风险溢价，但尚待市场响应验证。",
    ),
)
def test_language_preference_does_not_become_a_hardcoded_validity_gate(
    text: str,
) -> None:
    output = AssessStructuredOutput.model_validate(_world_output_payload(text))

    assert output.assessment.hypotheses[0].claim == text


def test_assessment_view_outcome_charges_latency_and_settles_once() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    create_market_schema(engine)
    evidence = SqlContextAssessmentStore(engine)
    packet = _packet()
    assessment = _assessment()
    evidence.record_packet(packet)
    evidence.record_assessment(packet.packet_id, assessment)
    market = SqlMarketDataStore(engine)
    reference = _reference_trade()
    evaluation_at = assessment.available_at + timedelta(minutes=240)
    exit_trade = _reference_trade(
        trade_id="trade-exit-1",
        aggregate_trade_id=2,
        event_time=evaluation_at,
        observed_at=evaluation_at,
        price=Decimal("70710.10"),
    )
    market.put_trade(reference)
    market.put_trade(exit_trade)
    store = SqlAssessmentViewOutcomeStore(engine)
    settler = AssessmentViewOutcomeSettler(
        engine=engine,
        store=store,
        evaluation_version="assessment-outcome-v1",
        maximum_market_age_seconds=30,
        settlement_grace_minutes=5,
    )

    pending_query = {
        "analysis_behavior_hash": assessment.analysis_behavior_hash,
        "evaluation_version": "assessment-outcome-v1",
        "signal_window_start": NOW,
        "signal_window_end": NOW + timedelta(hours=1),
    }
    assert store.pending_assessment_count(**pending_query) == 1
    before_maturity = settler.settle(as_of=evaluation_at - timedelta(seconds=1))
    settled = settler.settle(as_of=evaluation_at + timedelta(seconds=1))
    replayed = settler.settle(as_of=evaluation_at + timedelta(seconds=2))

    assert before_maturity.pending == 1
    assert settled.settled == 1
    assert replayed.settled == 0
    assert store.pending_assessment_count(**pending_query) == 0
    with engine.connect() as connection:
        payload = connection.execute(select(assessment_view_outcomes.c.payload)).scalar_one()
    outcome = AssessmentViewOutcome.model_validate(payload)
    assert outcome.reference_price == Decimal("70010")
    assert outcome.exit_price == Decimal("70710.10")
    assert outcome.market_return_bps == Decimal("100")
    assert outcome.directional_return_bps == Decimal("100")
    assert outcome.signal_observed_at == assessment.available_at
    assert store.visible_outcomes(
        analysis_behavior_hash=assessment.analysis_behavior_hash,
        evaluation_version="assessment-outcome-v1",
        signal_window_start=NOW,
        signal_window_end=NOW + timedelta(hours=1),
        published_at=evaluation_at + timedelta(seconds=1),
    ) == (outcome,)


def test_missing_signal_time_market_data_is_unscorable_not_packet_price() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    create_market_schema(engine)
    evidence = SqlContextAssessmentStore(engine)
    packet = _packet()
    assessment = _assessment()
    evidence.record_packet(packet)
    evidence.record_assessment(packet.packet_id, assessment)
    evaluation_at = assessment.available_at + timedelta(minutes=240)
    store = SqlAssessmentViewOutcomeStore(engine)

    result = AssessmentViewOutcomeSettler(
        engine=engine,
        store=store,
        evaluation_version="assessment-outcome-v1",
        maximum_market_age_seconds=30,
        settlement_grace_minutes=5,
    ).settle(as_of=evaluation_at + timedelta(minutes=6))

    assert result.unscorable == 1
    with engine.connect() as connection:
        payload = connection.execute(select(assessment_view_outcomes.c.payload)).scalar_one()
    outcome = AssessmentViewOutcome.model_validate(payload)
    assert outcome.reference_price is None
    assert outcome.reason_code == ("REFERENCE_MARKET_DATA_MISSING_AT_ASSESSMENT_AVAILABILITY")


class _CountingContextAnalyst:
    def __init__(self, assessment: ContextAssessment) -> None:
        self.assessment = assessment
        self.calls = 0

    def behavior_hash(self, packet: DecisionPacket) -> str:
        return self.assessment.analysis_behavior_hash

    def assess(self, packet: DecisionPacket) -> AnalystResult:
        self.calls += 1
        return AnalystResult(
            success=True,
            output=self.assessment,
            reason_code="CODEX_OK",
            account_id=".codex",
            run_id="assess-run-1",
        )


class _FailingContextAnalyst:
    def __init__(self) -> None:
        self.calls = 0

    def behavior_hash(self, packet: DecisionPacket) -> str:
        return "b" * 64

    def assess(self, packet: DecisionPacket) -> AnalystResult:
        self.calls += 1
        return AnalystResult(
            success=False,
            output=None,
            reason_code="CODEX_ACCOUNTS_UNAVAILABLE",
        )


def test_assessment_execution_replay_never_calls_codex_twice() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    assessment = _assessment()
    analyst = _CountingContextAnalyst(assessment)
    executor = ContextAssessmentExecutor(
        SqlContextAssessmentStore(engine),
        analyst,
    )

    first = executor.execute(_packet())
    replayed = executor.execute(_packet())

    assert first.status == AssessmentExecutionStatus.SUCCEEDED
    assert first.reused_authoritative is False
    assert replayed.status == AssessmentExecutionStatus.SUCCEEDED
    assert replayed.reused_authoritative is True
    assert replayed.assessment == first.assessment
    assert analyst.calls == 1
    with engine.connect() as connection:
        executions = tuple(connection.execute(select(assessment_executions.c.payload)).scalars())
    assert len(executions) == 2


def test_assessment_success_observer_is_retried_with_authoritative_result() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    assessment = _assessment()
    observed: list[str] = []
    executor = ContextAssessmentExecutor(
        SqlContextAssessmentStore(engine),
        _CountingContextAnalyst(assessment),
        on_success=lambda item: observed.append(item.assessment_id),
    )

    executor.execute(_packet())
    executor.execute(_packet())

    assert observed == [assessment.assessment_id, assessment.assessment_id]


def test_world_model_success_plans_one_idempotent_mechanism_review(app_config) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    assessments = SqlContextAssessmentStore(engine)
    packet = _packet()
    mechanism = ContextMechanism(
        mechanism_id="mechanism-review-1",
        relationship=ContextMechanismRelationship.SUPPORTS,
        claim="现货需求正在抵消衍生品卖压。",
        horizon_hours=24,
        causal_chain=(
            ContextCausalNode(
                statement="现货主动买盘高于卖盘。",
                evidence_ids=("fact-revision-1",),
            ),
            ContextCausalNode(
                statement="买盘开始抵消价格下行。",
                evidence_ids=("delta-1",),
            ),
        ),
        transmission_stage=ContextTransmissionStage.PROPAGATING,
        verification_tests=(
            ContextVerificationTest(
                feature_selector="asset_state:BTC.return_fraction",
                evaluation_window_minutes=240,
                supports_predicate=ContextVerificationPredicate(
                    operator="GTE",
                    value=Decimal("0"),
                ),
                contradicts_predicate=ContextVerificationPredicate(
                    operator="LT",
                    value=Decimal("0"),
                ),
            ),
        ),
        invalidation_conditions=("价格继续下跌且现货买盘转弱。",),
        next_review_at=NOW + timedelta(minutes=5),
    )
    world_model = ContextAssessment(
        schema_version=ContextAssessmentSchemaVersion.WORLD_MODEL_V2,
        assessment_id="world-model-review-1",
        analysis_scope=packet.analysis_scope,
        mandate_version=packet.mandate_version,
        as_of=packet.as_of,
        available_at=NOW + timedelta(seconds=20),
        analysis_behavior_hash="c" * 64,
        decision_packet_hash=packet.content_hash,
        trigger_ids=packet.trigger_ids,
        synthesis="加密内部卖压仍占主导，现货需求提供抵消。",
        synthesis_horizon_hours=24,
        mechanisms=(mechanism,),
    )
    assessments.record_packet(packet)
    assessments.record_assessment(packet.packet_id, world_model)
    triggers = SqlTriggerRepository(engine, app_config.trigger)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-review-v1",
        manifest_id="manifest-review-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    triggers.create_plan(plan)
    scheduler = WorldModelReviewScheduler(
        assessments=assessments,
        triggers=triggers,
        symbol="BTCUSDT",
        pipeline_id=plan.pipeline_id,
        manifest_id=plan.manifest_id,
        minimum_call_interval_seconds=app_config.trigger.minimum_call_interval_seconds,
        trigger_expiry_seconds=app_config.trigger.trigger_expiry_seconds,
        clock=lambda: NOW + timedelta(seconds=30),
    )

    scheduler.reconcile_latest(packet.analysis_scope)
    first = triggers.plan_for_scope(symbol="BTCUSDT", pipeline_id=plan.pipeline_id)
    scheduler.schedule(world_model)
    replayed = triggers.plan_for_scope(symbol="BTCUSDT", pipeline_id=plan.pipeline_id)

    assert replayed == first
    assert first.revision == 2
    assert len(first.scheduled_wakeups) == 1
    wakeup = first.scheduled_wakeups[0]
    assert wakeup.wake_at == mechanism.next_review_at
    assert wakeup.hypothesis == f"{WORLD_MODEL_REVIEW_MARKER}{world_model.assessment_id}"


def test_assessment_command_identity_covers_packet_and_behavior() -> None:
    command = AssessmentCommand.create(
        packet=_packet(),
        analysis_behavior_hash="b" * 64,
    )
    tampered = command.model_dump(mode="json")
    tampered["analysis_behavior_hash"] = "c" * 64

    with pytest.raises(ValidationError, match="command_hash"):
        AssessmentCommand.model_validate(tampered)


def test_assessment_application_rejects_runtime_behavior_drift() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    analyst = _CountingContextAnalyst(_assessment())
    application = AssessmentApplication(
        ContextAssessmentExecutor(SqlContextAssessmentStore(engine), analyst)
    )
    command = AssessmentCommand.create(
        packet=_packet(),
        analysis_behavior_hash="c" * 64,
    )

    with pytest.raises(ValueError, match="行为身份"):
        application.execute(command)
    assert analyst.calls == 0


def test_assessment_temporal_replay_reuses_authoritative_result(app_config) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        analyst = _CountingContextAnalyst(_assessment())
        application = AssessmentApplication(
            ContextAssessmentExecutor(SqlContextAssessmentStore(engine), analyst)
        )
        policy = app_config.temporal.model_copy(
            update={"assessment_task_queue": "assessment-workflow-test"}
        )
        command = AssessmentCommand.create(
            packet=_packet(),
            analysis_behavior_hash="b" * 64,
        )
        created_at = datetime.now(UTC)
        request = AssessmentWorkflowRequest.create(
            command=command,
            orchestration=OrchestrationPolicySnapshot.from_config(policy),
            created_at=created_at,
            deadline=created_at + timedelta(minutes=5),
        )
        async with await WorkflowEnvironment.start_time_skipping() as env:
            coordinator = AssessmentTemporalCoordinator(env.client, policy)
            async with AssessmentTemporalWorker(
                env.client,
                policy,
                application,
                worker_threads=1,
            ):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)

        assert first.status == AssessmentWorkflowStatus.SUCCEEDED
        assert replayed == first
        assert first.execution is not None
        assert first.execution.reused_authoritative is False
        assert analyst.calls == 1

    asyncio.run(scenario())


def test_assessment_business_failure_is_not_blindly_retried(app_config) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        analyst = _FailingContextAnalyst()
        application = AssessmentApplication(
            ContextAssessmentExecutor(SqlContextAssessmentStore(engine), analyst)
        )
        policy = app_config.temporal.model_copy(
            update={"assessment_task_queue": "assessment-no-result-test"}
        )
        command = AssessmentCommand.create(
            packet=_packet(),
            analysis_behavior_hash="b" * 64,
        )
        created_at = datetime.now(UTC)
        request = AssessmentWorkflowRequest.create(
            command=command,
            orchestration=OrchestrationPolicySnapshot.from_config(policy),
            created_at=created_at,
            deadline=created_at + timedelta(minutes=5),
        )
        async with await WorkflowEnvironment.start_time_skipping() as env:
            coordinator = AssessmentTemporalCoordinator(env.client, policy)
            async with AssessmentTemporalWorker(
                env.client,
                policy,
                application,
                worker_threads=1,
            ):
                result = await coordinator.execute(request)

        assert result.status == AssessmentWorkflowStatus.NO_ASSESSMENT
        assert result.attempt == 1
        assert result.reason_code == "CODEX_ACCOUNTS_UNAVAILABLE"
        assert analyst.calls == 1

    asyncio.run(scenario())
