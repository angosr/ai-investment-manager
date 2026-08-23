import asyncio
import json
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
from investment_manager.forecast.context.contract import (
    WorldModelStructuredOutput,
    assessment_input_projection,
)
from investment_manager.forecast.context.estimate import (
    ContextForecastStructuredOutput,
    ContextForecastTargetState,
)
from investment_manager.forecast.context.executor import (
    AssessmentExecutionStatus,
    ContextAssessmentExecutor,
)
from investment_manager.forecast.context.producer import (
    ContextForecastProducer,
    MarketContextTargetStateProvider,
    context_spot_forecast_contract,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.service import (
    WORLD_MODEL_REVIEW_MARKER,
    AssessmentTemporalCoordinator,
    AssessmentTemporalWorker,
    WorldModelReviewScheduler,
)
from investment_manager.forecast.context.workflow import AssessmentWorkflowRequest
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastNoEstimate,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextCausalNode,
    ContextMechanism,
    ContextMechanismRelationship,
    ContextTransmissionStage,
    ContextVerificationPredicate,
    ContextVerificationTest,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast
from investment_manager.forecast.tables import assessment_executions
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import (
    ClosedMarketBar,
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
    MarketTrade,
)
from investment_manager.market.perpetual.models import PerpetualMarketState, PerpetualQuote
from investment_manager.market.repository import InMemoryMarketDataStore, SqlMarketDataStore
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.models import build_initial_trigger_plan
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.schema import create_schema
from investment_manager.settings import load_config
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
        synthesis="政策修订正在改变贴现率路径。",
        synthesis_horizon_hours=24,
        mechanisms=(
            ContextMechanism(
                mechanism_id="mechanism-1",
                relationship=ContextMechanismRelationship.SUPPORTS,
                claim="政策修订正在改变贴现率路径。",
                horizon_hours=24,
                causal_chain=(
                    ContextCausalNode(statement="政策发生修订。", evidence_ids=("delta-1",)),
                    ContextCausalNode(statement="贴现率路径改变。", evidence_ids=("delta-1",)),
                ),
                transmission_stage=ContextTransmissionStage.PROPAGATING,
                verification_tests=(
                    ContextVerificationTest(
                        feature_selector="asset_state:BTC.return_fraction",
                        evaluation_window_minutes=240,
                        supports_predicate=ContextVerificationPredicate(
                            operator="GT", value=Decimal("0")
                        ),
                        contradicts_predicate=ContextVerificationPredicate(
                            operator="LT", value=Decimal("0")
                        ),
                    ),
                ),
                invalidation_conditions=("政策撤回",),
                next_review_at=NOW + timedelta(hours=1),
            ),
        ),
    )


def _world_output_payload(claim: str) -> dict:
    return {
        "world_model": {
            "synthesis": claim,
            "synthesis_horizon_hours": 24,
            "mechanisms": [
                {
                    "continuity_ref": None,
                    "relationship": "SUPPORTS",
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
                    "transmission_stage": "PROPAGATING",
                    "conflicting_evidence_ids": [],
                    "verification_tests": [
                        {
                            "feature_selector": "asset_state:BTC.return_fraction",
                            "evaluation_window_minutes": 240,
                            "supports_predicate": {"operator": "GT", "value": "0"},
                            "contradicts_predicate": {"operator": "LT", "value": "0"},
                        }
                    ],
                    "invalidation_conditions": ["政策变化被正式撤回"],
                    "next_review_at": (NOW + timedelta(hours=1)).isoformat(),
                }
            ],
            "event_relevance_updates": [],
        }
    }


def test_assessment_output_boundary_rejects_duplicate_evidence_items() -> None:
    payload = _world_output_payload("政策变化正在通过风险溢价影响资产定价。")
    payload["world_model"]["mechanisms"][0]["causal_chain"][0]["evidence_ids"] = [
        "delta-1",
        "delta-1",
    ]

    with pytest.raises(ValidationError, match="不能重复引用证据"):
        WorldModelStructuredOutput.model_validate(payload)


def test_assessment_output_requires_at_least_one_mechanism() -> None:
    payload = _world_output_payload("当前最可能的解释。")
    payload["world_model"]["mechanisms"] = []

    with pytest.raises(ValidationError, match="at least 1"):
        WorldModelStructuredOutput.model_validate(payload)


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
    output = WorldModelStructuredOutput.model_validate(_world_output_payload(text))

    assert output.world_model.mechanisms[0].claim == text


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


class _FixedProbabilityAnalyst:
    def __init__(self, completed_at: datetime) -> None:
        self.completed_at = completed_at
        self.calls = 0

    def estimate(self, *, slot, assessment, packet, target_state) -> AnalystResult:
        self.calls += 1
        return AnalystResult(
            success=True,
            output=ContextForecastStructuredOutput.model_validate(
                {
                    "forecast": {
                        "decision_slot_id": slot.slot_id,
                        "outcome_probabilities": [
                            {"bucket_id": "LARGE_LOSS", "probability": "0.05"},
                            {"bucket_id": "LOSS", "probability": "0.10"},
                            {"bucket_id": "FLAT", "probability": "0.20"},
                            {"bucket_id": "GAIN", "probability": "0.35"},
                            {"bucket_id": "LARGE_GAIN", "probability": "0.30"},
                        ],
                        "mechanism_contributions": [
                            {
                                "mechanism_id": assessment.mechanisms[0].mechanism_id,
                                "effect": "UPSIDE",
                                "rationale": "政策传导对未来四小时风险偏好形成可证伪上行贡献。",
                            }
                        ],
                        "evidence_refs": ["delta-1"],
                        "invalidation_conditions": ["政策传导在目标窗口内被市场响应反驳"],
                    }
                }
            ),
            reason_code="CODEX_OK",
            completed_at=self.completed_at,
        )


class _PacketTargetStateProvider:
    def __init__(self, packet: DecisionPacket) -> None:
        self.packet = packet

    def build(self, *, as_of: datetime) -> ContextForecastTargetState:
        return ContextForecastTargetState(
            as_of=as_of,
            asset_states=self.packet.asset_states,
            derivative_states=self.packet.derivative_states,
            input_refs=("target-state-test-ref",),
        )


def _context_forecast_producer(engine, analyst) -> ContextForecastProducer:
    config = load_config("config/investment-manager.yaml")
    policy = config.capital.context_forecast
    assert policy is not None
    instrument = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.key == policy.target_instrument_key
    )
    contract = context_spot_forecast_contract(
        policy=policy,
        instrument=instrument,
        cost_semantics_version=config.capital.decision.cost_model_version,
    )
    binding = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.CONTEXT.value,
            policy.producer_id,
            policy.producer_behavior_id,
            ForecastPermission.MOCK.value,
            policy.required_feature_keys,
            policy.maximum_world_model_age_seconds,
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id=policy.producer_id,
        producer_behavior_id=policy.producer_behavior_id,
        permission=ForecastPermission.MOCK,
        required_feature_keys=policy.required_feature_keys,
        maximum_world_model_age_seconds=policy.maximum_world_model_age_seconds,
    )
    packet = _packet()
    return ContextForecastProducer(
        policy=policy,
        contract=contract,
        binding=binding,
        market=SqlMarketDataStore(engine),
        contexts=SqlContextAssessmentStore(engine),
        contracts=SqlForecastContractStore(engine),
        forecasts=SqlForecastStore(engine),
        instrument=instrument,
        analyst=analyst,
        target_states=_PacketTargetStateProvider(packet),
        analysis_scope="crypto-portfolio",
    )


def test_context_forecast_persists_one_replay_safe_probability_result() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contexts = SqlContextAssessmentStore(engine)
    packet = contexts.record_packet(_packet())
    assessment = contexts.record_assessment(packet.packet_id, _assessment())
    completed_at = assessment.available_at + timedelta(seconds=10)
    analyst = _FixedProbabilityAnalyst(completed_at)
    producer = _context_forecast_producer(engine, analyst)
    market = SqlMarketDataStore(engine)
    for index, observed_at in enumerate((NOW, completed_at), start=1):
        market.put_quote(
            MarketQuote(
                quote_id=f"context-quote-{index}",
                symbol="BTCUSDT",
                observed_at=observed_at,
                bid=Decimal("69999"),
                bid_quantity=Decimal("10"),
                ask=Decimal("70001"),
                ask_quantity=Decimal("10"),
                update_id=index,
                source="test",
            )
        )

    first = producer.produce(as_of=assessment.available_at)
    replayed = producer.produce(as_of=assessment.available_at)

    assert isinstance(first, BaseForecast)
    assert replayed == first
    assert first.world_model_id == assessment.assessment_id
    assert first.expected_gross_bps == Decimal("87.5")
    assert first.mechanism_contributions[0].mechanism_id == "mechanism-1"
    assert first.evidence_refs == ("delta-1",)
    assert first.analysis_input_json is not None
    analysis_input = json.loads(first.analysis_input_json)
    assert analysis_input["purpose"] == "FORECAST_ESTIMATE"
    assert datetime.fromisoformat(
        analysis_input["decision_slot"]["information_cutoff_at"]
    ) == assessment.available_at
    assert datetime.fromisoformat(analysis_input["target_state"]["as_of"]) == (
        assessment.available_at
    )
    assert analysis_input["world_model"]["assessment_id"] == assessment.assessment_id
    assert "portfolio" not in assessment_input_projection(packet)
    assert "data_quality_codes" not in analysis_input["target_state"]
    assert "verification_tests" not in analysis_input["world_model"]["mechanisms"][0]
    assert "next_review_at" not in analysis_input["world_model"]["mechanisms"][0]
    assert "continuity_ref" not in analysis_input["world_model"]["mechanisms"][0]
    assert first.analysis_input_hash == content_hash(analysis_input)
    assert analyst.calls == 1


def test_context_forecast_records_stale_world_model_without_calling_codex() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contexts = SqlContextAssessmentStore(engine)
    packet = contexts.record_packet(_packet())
    contexts.record_assessment(packet.packet_id, _assessment())
    analyst = _FixedProbabilityAnalyst(NOW + timedelta(hours=2))
    producer = _context_forecast_producer(engine, analyst)
    SqlMarketDataStore(engine).put_quote(
        MarketQuote(
            quote_id="context-stale-cutoff",
            symbol="BTCUSDT",
            observed_at=NOW,
            bid=Decimal("69999"),
            bid_quantity=Decimal("10"),
            ask=Decimal("70001"),
            ask_quantity=Decimal("10"),
            update_id=1,
            source="test",
        )
    )

    result = producer.produce(as_of=NOW + timedelta(hours=2))

    assert isinstance(result, ForecastNoEstimate)
    assert result.reason.value == "WORLD_MODEL_STALE"
    assert result.detail == "WORLD_MODEL_MECHANISM_REVIEW_DUE"
    assert analyst.calls == 0


def test_context_forecast_target_state_is_rebuilt_at_the_slot() -> None:
    config = load_config("config/investment-manager.yaml")
    policy = config.capital.context_forecast
    assert policy is not None
    spot = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.key == policy.target_instrument_key
    )
    market = InMemoryMarketDataStore()
    closes = (Decimal("70000"), Decimal("70100"), Decimal("70200"))
    for index, close in enumerate(closes):
        open_at = NOW - timedelta(minutes=15 - index * 5)
        market.put_bar(
            ClosedMarketBar(
                symbol=spot.symbol,
                interval="5m",
                open_time=open_at,
                close_time=open_at + timedelta(minutes=5),
                observed_at=open_at + timedelta(minutes=5),
                open=close - Decimal("20"),
                high=close + Decimal("30"),
                low=close - Decimal("30"),
                close=close,
                volume=Decimal("10") + index,
                source="test",
            )
        )
    market.put_quote(
        MarketQuote(
            quote_id="fresh-target-quote",
            symbol=spot.symbol,
            observed_at=NOW,
            bid=Decimal("70199"),
            bid_quantity=Decimal("2"),
            ask=Decimal("70201"),
            ask_quantity=Decimal("2"),
            source="test",
        )
    )
    market.put_trade(
        MarketTrade(
            trade_id="fresh-target-trade",
            symbol=spot.symbol,
            aggregate_trade_id=1,
            event_time=NOW,
            observed_at=NOW,
            price=Decimal("70200"),
            quantity=Decimal("0.1"),
            buyer_is_maker=False,
            source="test",
        )
    )
    perpetual = InstrumentId(
        product=InstrumentProduct.USD_M_PERPETUAL,
        symbol=spot.symbol,
        base_asset=spot.base_asset,
        quote_asset=spot.quote_asset,
        settlement_asset=spot.quote_asset,
    )
    exchange_time = NOW - timedelta(seconds=1)
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                perpetual.key,
                exchange_time.isoformat(),
            ),
            instrument=perpetual,
            exchange_time=exchange_time,
            observed_at=NOW,
            mark_price=Decimal("70205"),
            index_price=Decimal("70200"),
            last_funding_rate=Decimal("0.0001"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=NOW + timedelta(hours=4),
            source="test",
        )
    )
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, 42),
            instrument=perpetual,
            exchange_time=exchange_time,
            observed_at=NOW,
            bid=Decimal("70204"),
            bid_quantity=Decimal("2"),
            ask=Decimal("70206"),
            ask_quantity=Decimal("2"),
            update_id=42,
            source="test",
        )
    )

    state = MarketContextTargetStateProvider(
        market=market,
        feature_policy=config.feature,
        spot=spot,
        perpetual=perpetual,
        interval="5m",
        bar_window=3,
        funding_lookback_hours=24,
        maximum_quote_skew_seconds=15,
    ).build(as_of=NOW)

    assert state.as_of == NOW
    assert state.asset_states[0].last == Decimal("70200")
    assert state.asset_states[0].return_fraction > 0
    assert state.derivative_states[0].mark_index_premium_bps > 0
    assert "asset_state:BTC.realized_volatility" in state.feature_selectors


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
    scheduler.publish_update(world_model)
    scheduler.publish_update(world_model)
    replayed = triggers.plan_for_scope(symbol="BTCUSDT", pipeline_id=plan.pipeline_id)

    assert replayed == first
    assert first.revision == 2
    assert len(first.scheduled_wakeups) == 1
    wakeup = first.scheduled_wakeups[0]
    assert wakeup.wake_at == mechanism.next_review_at
    assert wakeup.hypothesis == f"{WORLD_MODEL_REVIEW_MARKER}{world_model.assessment_id}"
    update_messages = tuple(
        item
        for item in triggers.pending_outbox(
            as_of=world_model.available_at + timedelta(minutes=20)
        )
        if item.message_kind == "TRIGGER_CREATED"
        and item.payload["trigger"]["trigger_type"] == "WORLD_MODEL_UPDATED"
    )
    assert len(update_messages) == 1
    assert update_messages[0].payload["trigger"]["evidence_ids"] == [
        world_model.assessment_id
    ]


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
