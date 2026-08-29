import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.decision_cycle.trigger import TriggerDispatchBuilder
from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.posterior_analyst import (
    CodexContextPosteriorAnalyst,
    PosteriorRunBundleBuilder,
)
from investment_manager.forecast.context.posterior_contract import (
    ContextPosteriorInput,
    ContextPosteriorStructuredOutput,
    PosteriorBucketDraft,
    PosteriorPriorTarget,
    PosteriorTargetDraft,
    finalize_posterior,
    posterior_output_schema,
)
from investment_manager.forecast.context.posterior_execution import (
    ContextPosteriorApplication,
    PosteriorExecutionStatus,
)
from investment_manager.forecast.context.posterior_preparation import (
    ContextPosteriorPreparation,
)
from investment_manager.forecast.context.posterior_workflow import (
    ContextPosteriorWorkflow,
    PosteriorWorkflowExecution,
    PosteriorWorkflowRequest,
    PosteriorWorkflowStatus,
)
from investment_manager.forecast.context.service import AssessmentTemporalWorker
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import ForecastPriceAnchor
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextCausalNode,
    ContextMechanism,
    ContextMechanismRelationship,
    ContextTransmissionStage,
    ContextVerificationPredicate,
    ContextVerificationTest,
)
from investment_manager.forecast.program.baseline import load_forecast_baseline
from investment_manager.forecast.program.prior import RollingPriorForecastProducer
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    ForecastMechanismContribution,
    ForecastMechanismEffect,
)
from investment_manager.forecast.tables import forecasts as forecast_rows
from investment_manager.governance.policy import DeploymentStage
from investment_manager.market.models import MarketQuote
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
)
from investment_manager.schema import create_schema

SLOT_AT = datetime(2026, 8, 30, tzinfo=UTC)


def _prior_targets():
    root = Path(__file__).resolve().parents[1]
    artifact = load_forecast_baseline(
        root / "evidence/forecast-baselines/forecast_baseline_7edf2cf090b47cdad2e5.json"
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    contracts = SqlForecastContractStore(engine)
    forecasts = SqlForecastStore(engine)
    market = InMemoryMarketDataStore()
    for symbol, price in (("BTCUSDT", Decimal("110000")), ("PAXGUSDT", Decimal("3500"))):
        market.put_quote(
            MarketQuote(
                quote_id=f"{symbol}-cutoff",
                symbol=symbol,
                observed_at=SLOT_AT,
                bid=price,
                bid_quantity="1",
                ask=price + Decimal("1"),
                ask_quantity="1",
                source="test",
            )
        )
    prior = RollingPriorForecastProducer(
        artifact=artifact,
        market=market,
        contracts=contracts,
        forecasts=forecasts,
        outcome_evaluation_version="forecast-target-outcome-v1",
        activated_at=SLOT_AT - timedelta(hours=1),
        maximum_quote_age_seconds=300,
        clock=lambda: SLOT_AT + timedelta(minutes=1),
    ).produce(as_of=SLOT_AT + timedelta(minutes=1))
    targets = tuple(
        PosteriorPriorTarget(
            contract=contracts.contract(item.contract_id),
            slot=contracts.slot(item.decision_slot_id),
            prior=item,
        )
        for item in prior
    )
    return contracts, forecasts, targets, market, engine


def _world_model() -> ContextAssessment:
    mechanism = ContextMechanism(
        mechanism_id="structural-liquidity-1",
        relationship=ContextMechanismRelationship.SUPPORTS,
        claim="政策现金流正在改善风险资产可用流动性。",
        horizon_hours=72,
        causal_chain=(
            ContextCausalNode(statement="政策现金流已经变化。", evidence_ids=("fact-1",)),
            ContextCausalNode(statement="融资条件获得缓冲。", evidence_ids=("fact-1",)),
        ),
        transmission_stage=ContextTransmissionStage.PROPAGATING,
        verification_tests=(
            ContextVerificationTest(
                feature_selector="fact_state:liquidity.value",
                evaluation_window_minutes=4320,
                supports_predicate=ContextVerificationPredicate(operator="GT", value=Decimal("0")),
                contradicts_predicate=ContextVerificationPredicate(
                    operator="LT", value=Decimal("0")
                ),
            ),
        ),
        invalidation_conditions=("政策现金流反转",),
        next_review_at=SLOT_AT + timedelta(days=1),
    )
    return ContextAssessment(
        assessment_id="world-model-1",
        analysis_scope="primary-portfolio",
        mandate_version="mandate-v1",
        as_of=SLOT_AT - timedelta(hours=6),
        available_at=SLOT_AT - timedelta(hours=5),
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        trigger_ids=("trigger-1",),
        synthesis="政策现金流改善流动性，但仍需观察融资条件传导。",
        synthesis_horizon_hours=72,
        mechanisms=(mechanism,),
    )


def _input(*, eligible: bool = True) -> ContextPosteriorInput:
    return ContextPosteriorInput.create(
        information_cutoff_at=SLOT_AT,
        world_model=_world_model(),
        eligible_mechanism_ids=("structural-liquidity-1",) if eligible else (),
        targets=_prior_targets()[2],
    )


def _output(value: ContextPosteriorInput, *, change: bool, contribute: bool):
    drafts = []
    for target in value.targets:
        probabilities = [item.probability for item in target.prior.outcome_probabilities]
        if change:
            probabilities[0] -= Decimal("0.01")
            probabilities[-1] += Decimal("0.01")
        drafts.append(
            PosteriorTargetDraft(
                contract_id=target.contract.contract_id,
                buckets=tuple(
                    PosteriorBucketDraft(
                        bucket_id=bucket.bucket_id,
                        probability=probability,
                        rationale="结构流动性使尾部概率发生变化。"
                        if change
                        else "没有结构增量，保持先验。",
                    )
                    for bucket, probability in zip(
                        target.contract.outcome_buckets,
                        probabilities,
                        strict=True,
                    )
                ),
                mechanism_contributions=(
                    (
                        ForecastMechanismContribution(
                            mechanism_id="structural-liquidity-1",
                            effect=ForecastMechanismEffect.UPSIDE,
                            rationale="政策现金流通过融资条件传导。",
                        ),
                    )
                    if contribute
                    else ()
                ),
            )
        )
    return ContextPosteriorStructuredOutput(forecasts=tuple(drafts))


def _entry_anchors(value: ContextPosteriorInput, completed_at: datetime):
    return {
        target.contract.contract_id: (
            ForecastPriceAnchor(
                instrument_id=target.contract.target.legs[0].instrument.key,
                price=target.prior.entry_prices[0].price,
                observed_at=completed_at,
                available_at=completed_at,
                quote_ref=f"entry-{target.contract.contract_id}",
            ),
        )
        for target in value.targets
    }


def test_joint_posterior_preserves_frozen_input_output_and_mechanism_lineage() -> None:
    frozen = _input()
    completed = SLOT_AT + timedelta(minutes=10)
    output = _output(frozen, change=True, contribute=True)

    forecasts = finalize_posterior(
        output=output,
        frozen_input=frozen,
        producer_behavior_id="posterior-behavior-v1",
        completed_at=completed,
        entry_anchors=_entry_anchors(frozen, completed),
    )

    assert len(forecasts) == 2
    assert all(item.available_at == completed for item in forecasts)
    assert all(item.world_model_id == "world-model-1" for item in forecasts)
    assert all(item.analysis_input_json is not None for item in forecasts)
    assert all(item.analysis_output_json is not None for item in forecasts)
    assert all(item.evidence_refs == ("fact-1",) for item in forecasts)


def test_posterior_must_not_change_prior_without_structural_attribution() -> None:
    frozen = _input(eligible=False)
    completed = SLOT_AT + timedelta(minutes=10)

    with pytest.raises(ValueError, match="必须绑定结构机制"):
        finalize_posterior(
            output=_output(frozen, change=True, contribute=False),
            frozen_input=frozen,
            producer_behavior_id="posterior-behavior-v1",
            completed_at=completed,
            entry_anchors=_entry_anchors(frozen, completed),
        )


def test_unchanged_posterior_is_a_valid_context_forecast_without_fake_contribution() -> None:
    frozen = _input(eligible=False)
    completed = SLOT_AT + timedelta(minutes=10)

    forecasts = finalize_posterior(
        output=_output(frozen, change=False, contribute=False),
        frozen_input=frozen,
        producer_behavior_id="posterior-behavior-v1",
        completed_at=completed,
        entry_anchors=_entry_anchors(frozen, completed),
    )

    assert all(not item.mechanism_contributions for item in forecasts)
    assert all(not item.evidence_refs for item in forecasts)
    assert all(item.world_model_id == "world-model-1" for item in forecasts)


def test_posterior_schema_is_bounded_to_frozen_contracts_and_mechanisms() -> None:
    frozen = _input()
    schema = posterior_output_schema(frozen)
    definitions = schema["$defs"]

    assert definitions["PosteriorTargetDraft"]["properties"]["contract_id"]["enum"] == [
        item.contract.contract_id for item in frozen.targets
    ]
    assert definitions["ForecastMechanismContribution"]["properties"]["mechanism_id"]["enum"] == [
        "structural-liquidity-1"
    ]

    without_eligible = posterior_output_schema(_input(eligible=False))
    assert (
        without_eligible["$defs"]["PosteriorTargetDraft"]["properties"]["mechanism_contributions"][
            "maxItems"
        ]
        == 0
    )


class _PosteriorRouter:
    def __init__(self, output: ContextPosteriorStructuredOutput) -> None:
        self.output = output
        self.calls = 0

    def run(self, _bundle) -> AnalystResult:
        self.calls += 1
        return AnalystResult(
            True,
            self.output,
            "CODEX_ANALYSIS_SUCCEEDED",
            account_id=".codex2",
            attempts=1,
            completed_at=SLOT_AT + timedelta(minutes=10),
            run_id="codex-run-1",
        )


def test_posterior_analyst_freezes_bundle_and_uses_joint_behavior_identity(
    app_config,
    tmp_path,
) -> None:
    frozen = _input(eligible=False)
    contracts = tuple(item.contract for item in frozen.targets)
    runtime = app_config.codex_runtime
    builder = PosteriorRunBundleBuilder(
        runtime,
        contracts=contracts,
        code_version="test-code",
        configuration_hash="f" * 64,
    )
    router = _PosteriorRouter(_output(frozen, change=False, contribute=False))
    analyst = CodexContextPosteriorAnalyst(
        tmp_path,
        builder,
        router,
        maximum_schema_attempts=1,
    )

    result = analyst.forecast(frozen)

    assert result.success is True
    assert result.output == router.output
    assert router.calls == 1
    manifest = next(tmp_path.glob("*/manifest.json")).read_text(encoding="utf-8")
    assert '"analysis_mode":"CONTEXT_POSTERIOR"' in manifest
    assert frozen.input_hash in manifest

    subset = ContextPosteriorInput.create(
        information_cutoff_at=frozen.information_cutoff_at,
        world_model=frozen.world_model,
        eligible_mechanism_ids=(),
        targets=frozen.targets[:1],
    )
    assert analyst.behavior_hash(subset) == analyst.behavior_hash(frozen)


class _AssessmentFacts:
    def __init__(self, world_model):
        self.world_model = world_model

    def latest_before(self, *, analysis_scope, as_of):
        assert analysis_scope == "primary-portfolio"
        assert as_of == SLOT_AT
        return self.world_model

    def packet_for_assessment(self, assessment_id):
        assert assessment_id == self.world_model.assessment_id
        fact = type("Fact", (), {"revision_id": "fact-1"})()
        return type("Packet", (), {"facts": (fact,), "intelligence_events": ()})()

    def mechanism_observations(self, assessment_id):
        assert assessment_id == self.world_model.assessment_id
        return ()


def _preparation(base_app_config, world_model):
    contract_store, forecast_store, targets, market, engine = _prior_targets()
    contracts = tuple(
        sorted((item.contract for item in targets), key=lambda item: item.contract_id)
    )
    return (
        ContextPosteriorPreparation(
            contracts=contracts,
            runtime=base_app_config.codex_runtime,
            analysis_scope="primary-portfolio",
            activated_at=SLOT_AT - timedelta(hours=1),
            contract_store=contract_store,
            forecast_store=forecast_store,
            assessments=_AssessmentFacts(world_model),
        ),
        contract_store,
        forecast_store,
        targets,
        market,
        engine,
    )


def test_preparation_records_research_obligations_and_structural_eligibility(
    base_app_config,
) -> None:
    preparation, contract_store, _forecast_store, targets, _market, _engine = _preparation(
        base_app_config,
        _world_model(),
    )

    frozen = preparation.prepare(
        tuple(item.prior for item in targets),
        as_of=SLOT_AT + timedelta(minutes=2),
    )

    assert frozen is not None
    assert frozen.eligible_mechanism_ids == ("structural-liquidity-1",)
    for target in frozen.targets:
        binding = preparation.binding(target.contract)
        assert contract_store.latest_obligated_slot_at(binding_id=binding.binding_id) == SLOT_AT


def test_preparation_records_no_estimate_when_world_model_was_not_available(
    base_app_config,
) -> None:
    preparation, _contract_store, forecast_store, targets, _market, _engine = _preparation(
        base_app_config,
        None,
    )

    assert (
        preparation.prepare(
            tuple(item.prior for item in targets),
            as_of=SLOT_AT + timedelta(minutes=2),
        )
        is None
    )
    assert all(
        forecast_store.no_estimate_exists(
            decision_slot_id=item.slot.slot_id,
            producer_behavior_id=preparation.producer_behavior_id,
        )
        for item in targets
    )


class _ExecutionAnalyst:
    def __init__(self, behavior_hash: str, result: AnalystResult) -> None:
        self._behavior_hash = behavior_hash
        self.result = result
        self.calls = 0

    def behavior_hash(self, _value) -> str:
        return self._behavior_hash

    def forecast(self, _value) -> AnalystResult:
        self.calls += 1
        return self.result


def _execution_fixture(base_app_config):
    preparation, contracts, forecasts, targets, market, engine = _preparation(
        base_app_config,
        _world_model(),
    )
    frozen = preparation.prepare(
        tuple(item.prior for item in targets),
        as_of=SLOT_AT + timedelta(minutes=2),
    )
    assert frozen is not None
    completed_at = SLOT_AT + timedelta(minutes=10)
    for target in frozen.targets:
        instrument = target.contract.target.legs[0].instrument
        price = target.prior.entry_prices[0].price
        market.put_quote(
            MarketQuote(
                quote_id=f"{instrument.symbol}-posterior-entry",
                symbol=instrument.symbol,
                observed_at=completed_at,
                bid=price,
                bid_quantity="1",
                ask=price + Decimal("1"),
                ask_quantity="1",
                source="test",
            )
        )
    return preparation, contracts, forecasts, market, engine, frozen, completed_at


def test_posterior_execution_records_joint_forecasts_and_reuses_them(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, _engine, frozen, completed_at = _execution_fixture(
        base_app_config
    )
    analyst = _ExecutionAnalyst(
        preparation.producer_behavior_id,
        AnalystResult(
            True,
            _output(frozen, change=False, contribute=False),
            "CODEX_ANALYSIS_SUCCEEDED",
            attempts=1,
            completed_at=completed_at,
            run_id="posterior-run-1",
        ),
    )
    application = ContextPosteriorApplication(
        analyst=analyst,
        contracts=contracts,
        forecasts=forecasts,
        market=market,
        maximum_quote_age_seconds=300,
        clock=lambda: SLOT_AT + timedelta(minutes=3),
    )

    first = application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )
    replayed = application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )

    assert first.status == PosteriorExecutionStatus.SUCCEEDED
    assert len(first.forecast_ids) == len(frozen.targets)
    assert replayed.reused_authoritative is True
    assert replayed.forecast_ids == first.forecast_ids
    assert analyst.calls == 1


def test_posterior_execution_recovers_partial_joint_write_without_second_ai_call(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, engine, frozen, completed_at = _execution_fixture(
        base_app_config
    )
    analyst = _ExecutionAnalyst(
        preparation.producer_behavior_id,
        AnalystResult(
            True,
            _output(frozen, change=False, contribute=False),
            "CODEX_ANALYSIS_SUCCEEDED",
            attempts=1,
            completed_at=completed_at,
            run_id="posterior-run-1",
        ),
    )
    application = ContextPosteriorApplication(
        analyst=analyst,
        contracts=contracts,
        forecasts=forecasts,
        market=market,
        maximum_quote_age_seconds=300,
        clock=lambda: SLOT_AT + timedelta(minutes=3),
    )
    first = application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )
    with engine.begin() as connection:
        connection.execute(
            delete(forecast_rows).where(forecast_rows.c.forecast_id == first.forecast_ids[-1])
        )

    recovered = application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )

    assert recovered.status == PosteriorExecutionStatus.SUCCEEDED
    assert recovered.reused_authoritative is True
    assert recovered.forecast_ids == first.forecast_ids
    assert analyst.calls == 1


def test_posterior_business_failure_records_terminal_no_estimates(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, _engine, frozen, completed_at = _execution_fixture(
        base_app_config
    )
    analyst = _ExecutionAnalyst(
        preparation.producer_behavior_id,
        AnalystResult(
            False,
            None,
            "CODEX_ACCOUNTS_UNAVAILABLE",
            attempts=3,
            completed_at=completed_at,
        ),
    )
    application = ContextPosteriorApplication(
        analyst=analyst,
        contracts=contracts,
        forecasts=forecasts,
        market=market,
        maximum_quote_age_seconds=300,
        clock=lambda: SLOT_AT + timedelta(minutes=3),
    )

    result = application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )
    replayed = application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )

    assert result.status == PosteriorExecutionStatus.NO_ESTIMATE
    assert len(result.no_estimate_ids) == len(frozen.targets)
    assert replayed.reused_authoritative is True
    assert analyst.calls == 1


class _UnusedAssessmentApplication:
    def execute(self, _command):
        raise AssertionError("Posterior Workflow 不应调用 ContextAssessment application")


def test_existing_assessment_worker_executes_posterior_workflow(
    base_app_config,
) -> None:
    async def scenario() -> None:
        preparation, contracts, forecasts, market, _engine, frozen, completed_at = (
            _execution_fixture(base_app_config)
        )
        analyst = _ExecutionAnalyst(
            preparation.producer_behavior_id,
            AnalystResult(
                True,
                _output(frozen, change=False, contribute=False),
                "CODEX_ANALYSIS_SUCCEEDED",
                attempts=1,
                completed_at=completed_at,
                run_id="posterior-temporal-run-1",
            ),
        )
        application = ContextPosteriorApplication(
            analyst=analyst,
            contracts=contracts,
            forecasts=forecasts,
            market=market,
            maximum_quote_age_seconds=300,
            clock=lambda: SLOT_AT + timedelta(minutes=3),
        )
        policy = base_app_config.temporal.model_copy(
            update={"assessment_task_queue": "posterior-workflow-test"}
        )
        request = PosteriorWorkflowRequest.create(
            frozen_input=frozen,
            producer_behavior_id=preparation.producer_behavior_id,
            orchestration=OrchestrationPolicySnapshot.from_config(policy),
            created_at=SLOT_AT + timedelta(minutes=2),
        )
        async with (
            await WorkflowEnvironment.start_time_skipping() as environment,
            AssessmentTemporalWorker(
                environment.client,
                policy,
                _UnusedAssessmentApplication(),
                posterior_application=application,
                worker_threads=1,
            ),
        ):
            raw = await environment.client.execute_workflow(
                ContextPosteriorWorkflow.run,
                request.model_dump(mode="json"),
                id=request.workflow_id,
                task_queue=policy.assessment_task_queue,
            )
        result = PosteriorWorkflowExecution.model_validate(raw)
        assert result.status == PosteriorWorkflowStatus.SUCCEEDED
        assert result.attempt == 1
        assert result.execution is not None
        assert len(result.execution.forecast_ids) == len(frozen.targets)

    asyncio.run(scenario())


def test_trigger_dispatches_one_joint_posterior_from_program_prior(app_config) -> None:
    frozen = _input()

    class PriorProducer:
        def produce(self, *, as_of):
            assert as_of == SLOT_AT + timedelta(minutes=1)
            return tuple(item.prior for item in frozen.targets)

    class PosteriorPreparation:
        producer_behavior_id = "posterior-behavior-v1"

        def prepare(self, prior_results, *, as_of):
            assert prior_results == tuple(item.prior for item in frozen.targets)
            assert as_of == SLOT_AT + timedelta(minutes=1)
            return frozen

    config = app_config.model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": False}),
            "deployment": app_config.deployment.model_copy(
                update={
                    "stage": DeploymentStage.SHADOW,
                    "shadow_market_data_enabled": True,
                }
            ),
        }
    )
    plan = build_initial_trigger_plan(
        symbol=config.assessment.review_trigger_symbol,
        pipeline_id=config.pipeline.version,
        manifest_id="posterior-release-v1",
        updated_at=SLOT_AT,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.FORECAST_SLOT_DUE,
        symbol=config.assessment.review_trigger_symbol,
        pipeline_id=config.pipeline.version,
        occurred_at=SLOT_AT + timedelta(minutes=1),
        observed_at=SLOT_AT + timedelta(minutes=1),
        priority=1,
        dedup_key="posterior-slot-v1",
    )
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=SLOT_AT + timedelta(minutes=1),
        deadline=SLOT_AT + timedelta(minutes=15),
    )

    dispatches = TriggerDispatchBuilder(
        config=config,
        program_forecast_producers=(PriorProducer(),),
        posterior_preparation=PosteriorPreparation(),
    ).build(batch)

    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert dispatch.workflow_name == "ContextPosteriorWorkflow"
    request = PosteriorWorkflowRequest.model_validate(dispatch.payload)
    assert request.frozen_input == frozen
    assert request.producer_behavior_id == "posterior-behavior-v1"
