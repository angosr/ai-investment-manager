import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.decision_cycle.trigger import TriggerDispatchBuilder
from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.executor import (
    AssessmentExecution,
    AssessmentExecutionStatus,
)
from investment_manager.forecast.context.increment_evidence import (
    ForecastIncrementStatus,
    SqlForecastIncrementEvidenceReader,
)
from investment_manager.forecast.context.posterior_analyst import (
    CodexContextPosteriorAnalyst,
    PosteriorRunBundleBuilder,
)
from investment_manager.forecast.context.posterior_contract import (
    ContextPosteriorInput,
    ContextPosteriorSeed,
    ContextPosteriorStructuredOutput,
    PosteriorBucketDraft,
    PosteriorPriorTarget,
    PosteriorTargetDraft,
    build_posterior_prompt,
    finalize_posterior,
    posterior_analysis_projection,
    posterior_output_schema,
)
from investment_manager.forecast.context.posterior_execution import (
    ContextAssessmentPosteriorApplication,
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
from investment_manager.forecast.contracts import (
    MATERIAL_SLOT_POLICY_VERSION,
    ForecastNoEstimateReason,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
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
from investment_manager.forecast.program.baseline import load_forecast_baseline
from investment_manager.forecast.program.prior import (
    PRIOR_PRODUCER_ID,
    RollingPriorForecastProducer,
    build_prior_targets,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    ForecastLegOutcome,
    ForecastMechanismContribution,
    ForecastMechanismEffect,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.forecast.tables import (
    forecast_producer_bindings,
    forecast_slot_obligations,
)
from investment_manager.forecast.tables import (
    forecasts as forecast_rows,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.information.models import SourceTier
from investment_manager.kernel.identity import stable_id
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
from investment_manager.state.decision.packet import (
    DecisionPacket,
    MandateExposure,
    PacketAssetState,
    PacketFact,
    PacketPortfolioState,
    PacketReviewRequest,
)
from investment_manager.state.models import FactDecisionMateriality, FactRevisionStatus

SLOT_AT = datetime(2026, 8, 30, tzinfo=UTC)


def _packet() -> DecisionPacket:
    review = PacketReviewRequest.create(
        requested_at=SLOT_AT,
        reason="正式 Forecast 槽世界认知更新",
    )
    return DecisionPacket.create(
        schema_version="packet-v1",
        policy_version="packet-policy-v1",
        mandate_version="mandate-v1",
        analysis_scope="primary-portfolio",
        as_of=SLOT_AT,
        state_id="state-1",
        question="更新同一截止的组合世界认知。",
        trigger_ids=(review.review_id,),
        mandate_exposures=(MandateExposure(economic_exposure="CRYPTO_NETWORK", asset="BTC"),),
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
                observed_at=SLOT_AT,
                bid=Decimal("109999"),
                ask=Decimal("110001"),
                last=Decimal("110000"),
                return_fraction=Decimal("0"),
                realized_volatility=Decimal("0.3"),
                atr=Decimal("1000"),
                spread_bps=Decimal("0.2"),
                volume_ratio=Decimal("1"),
                regime="RANGE",
                market_age_seconds=0,
            ),
        ),
        deltas=(),
        facts=(
            PacketFact(
                fact_id="liquidity-fact",
                revision_id="fact-1",
                fact_type="liquidity",
                status=FactRevisionStatus.ACTIVE,
                event_time=SLOT_AT,
                observed_at=SLOT_AT,
                headline="政策现金流",
                claim="value=1 INDEX.",
                affected_assets=("BTC",),
                risk_factors=("LIQUIDITY",),
                decision_materiality=FactDecisionMateriality.CANDIDATE,
                highest_source_tier=SourceTier.FIRST_PARTY,
                independent_source_count=1,
                prompt_injection_suspected=False,
                directly_triggered=False,
            ),
        ),
        review_requests=(review,),
        data_quality_codes=(),
        coverage_gap_codes=(),
        missing_fact_revision_ids=(),
        omitted_fact_revision_ids=(),
    )


def _prior_runtime():
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
    )
    runtime_targets = tuple(
        sorted(build_prior_targets(artifact), key=lambda item: item.contract.contract_id)
    )
    return contracts, forecasts, market, engine, prior, runtime_targets


def _prior_targets():
    contracts, forecasts, market, engine, producer, _runtime_targets = _prior_runtime()
    prior = producer.produce(as_of=SLOT_AT + timedelta(minutes=1))
    targets = tuple(
        PosteriorPriorTarget(
            contract=contracts.contract(item.contract_id),
            slot=contracts.slot(item.decision_slot_id),
            prior=item,
        )
        for item in prior
    )
    return contracts, forecasts, targets, market, engine


def _world_model(*, decision_packet_hash: str | None = None) -> ContextAssessment:
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
        as_of=SLOT_AT,
        available_at=SLOT_AT + timedelta(minutes=5),
        analysis_behavior_hash="a" * 64,
        decision_packet_hash=decision_packet_hash or "b" * 64,
        trigger_ids=("trigger-1",),
        synthesis="政策现金流改善流动性，但仍需观察融资条件传导。",
        synthesis_horizon_hours=72,
        mechanisms=(mechanism,),
    )


def _prior_bindings(
    targets: tuple[PosteriorPriorTarget, ...],
) -> tuple[ForecastProducerBinding, ...]:
    return tuple(
        ForecastProducerBinding.create(
            contract_id=item.contract.contract_id,
            producer_kind=ForecastProducerKind.PROGRAM,
            producer_id=item.prior.producer_id,
            producer_behavior_id=item.prior.producer_behavior_id,
            permission=ForecastPermission.RESEARCH,
        )
        for item in sorted(targets, key=lambda value: value.contract.contract_id)
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
                mechanism_contributions=tuple(
                    ForecastMechanismContribution(
                        mechanism_id=mechanism_id,
                        effect=(
                            ForecastMechanismEffect.UPSIDE
                            if change and contribute
                            else ForecastMechanismEffect.NO_MATERIAL_EFFECT
                        ),
                        rationale=(
                            "政策现金流通过融资条件传导。"
                            if change and contribute
                            else "该机制未给本目标带来可归因增量。"
                        ),
                    )
                    for mechanism_id in value.eligible_mechanism_ids
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
    contribution_schema = definitions["PosteriorTargetDraft"]["properties"][
        "mechanism_contributions"
    ]
    assert contribution_schema["minItems"] == 1
    assert contribution_schema["maxItems"] == 1

    without_eligible = posterior_output_schema(_input(eligible=False))
    assert (
        without_eligible["$defs"]["PosteriorTargetDraft"]["properties"]["mechanism_contributions"][
            "maxItems"
        ]
        == 0
    )


def test_posterior_prompt_projects_decision_semantics_once_within_capacity() -> None:
    frozen = _input()
    projection = posterior_analysis_projection(frozen)
    prompt = build_posterior_prompt(frozen)

    assert len(prompt) < 16_000
    assert projection["eligible_mechanism_ids"] == ("structural-liquidity-1",)
    assert projection["world_model"]["eligible_mechanisms"][0]["claim"] == (
        frozen.world_model.mechanisms[0].claim
    )
    assert len(projection["targets"]) == 2
    assert "program_input_json" not in prompt
    assert "entry_prices" not in prompt
    assert "cutoff_prices" not in prompt
    assert "forecast_benchmark" not in prompt
    assert "active_events" not in prompt
    assert "causal_chain" not in prompt
    assert "各自 horizon_minutes" in prompt
    assert "72 小时收益桶" not in prompt


def test_posterior_prompt_capacity_is_independent_of_full_world_causal_depth() -> None:
    frozen = _input()
    template = frozen.world_model.mechanisms[0]
    mechanisms = tuple(
        template.model_copy(
            update={
                "mechanism_id": f"structural-mechanism-{index}",
                "claim": "组合含义" * 100,
                "causal_chain": tuple(
                    ContextCausalNode(
                        statement=f"不应在后验重复的完整因果节点{index}-{node}" + "链" * 500,
                        evidence_ids=(),
                    )
                    for node in range(5)
                ),
            }
        )
        for index in range(6)
    )
    world_model = frozen.world_model.model_copy(
        update={
            "synthesis": "联合组合结论" * 250,
            "mechanisms": mechanisms,
        }
    )
    maximum_world = ContextPosteriorInput.create(
        information_cutoff_at=frozen.information_cutoff_at,
        world_model=world_model,
        eligible_mechanism_ids=tuple(item.mechanism_id for item in mechanisms),
        targets=frozen.targets,
    )

    prompt = build_posterior_prompt(maximum_world)

    assert len(prompt) < 16_000
    assert "不应在后验重复的完整因果节点" not in prompt


def test_posterior_rejects_direction_inconsistent_with_mechanism() -> None:
    frozen = _input()
    completed = SLOT_AT + timedelta(minutes=10)
    output = _output(frozen, change=True, contribute=True)
    reversed_forecasts = []
    for forecast in output.forecasts:
        reversed_probabilities = [bucket.probability for bucket in forecast.buckets]
        reversed_probabilities[0] += Decimal("0.02")
        reversed_probabilities[-1] -= Decimal("0.02")
        reversed_forecasts.append(
            forecast.model_copy(
                update={
                    "buckets": tuple(
                        bucket.model_copy(update={"probability": probability})
                        for bucket, probability in zip(
                            forecast.buckets,
                            reversed_probabilities,
                            strict=True,
                        )
                    )
                }
            )
        )

    with pytest.raises(ValueError, match="单边 UPSIDE"):
        finalize_posterior(
            output=output.model_copy(update={"forecasts": tuple(reversed_forecasts)}),
            frozen_input=frozen,
            producer_behavior_id="posterior-behavior-v1",
            completed_at=completed,
            entry_anchors=_entry_anchors(frozen, completed),
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
        prior_bindings=_prior_bindings(frozen.targets),
        world_model_behavior_id="a" * 64,
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


def _preparation(base_app_config):
    contract_store, forecast_store, targets, market, engine = _prior_targets()
    contracts = tuple(
        sorted((item.contract for item in targets), key=lambda item: item.contract_id)
    )
    return (
        ContextPosteriorPreparation(
            contracts=contracts,
            prior_bindings=_prior_bindings(targets),
            runtime=base_app_config.codex_runtime,
            world_model_behavior_id="a" * 64,
            activated_at=SLOT_AT - timedelta(hours=1),
            contract_store=contract_store,
            forecast_store=forecast_store,
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
        base_app_config
    )

    seed = preparation.reserve(
        tuple(item.prior for item in targets),
        as_of=SLOT_AT + timedelta(minutes=2),
    )

    assert seed is not None
    for target in seed.targets:
        binding = preparation.binding(target.contract)
        assert contract_store.latest_obligated_slot_at(binding_id=binding.binding_id) == SLOT_AT


def test_posterior_behavior_identity_includes_world_model_behavior(base_app_config) -> None:
    preparation, *_rest = _preparation(base_app_config)
    changed = replace(preparation, world_model_behavior_id="b" * 64)

    assert changed.producer_behavior_id != preparation.producer_behavior_id


def test_equivalent_release_reuses_original_posterior_activation(base_app_config) -> None:
    preparation, contracts, forecasts, targets, _market, _engine = _preparation(base_app_config)
    prior_results = tuple(item.prior for item in targets)
    first = preparation.reserve(
        prior_results,
        as_of=SLOT_AT + timedelta(minutes=2),
    )
    rebound = replace(
        preparation,
        activated_at=SLOT_AT + timedelta(minutes=30),
        contract_store=contracts,
        forecast_store=forecasts,
    ).reserve(
        prior_results,
        as_of=SLOT_AT + timedelta(minutes=31),
    )

    assert first is not None
    assert rebound == first


def test_equivalent_release_reuses_activation_without_creating_an_obligation(
    base_app_config,
) -> None:
    preparation, contracts, _forecasts, _targets, _market, _engine = _preparation(base_app_config)

    bindings = preparation.activate()
    rebound = replace(
        preparation,
        activated_at=SLOT_AT + timedelta(minutes=30),
    ).activate()

    assert len(bindings) == 2
    assert rebound == bindings
    assert all(
        contracts.binding_activation_at(binding.binding_id) == preparation.activated_at
        for binding in bindings
    )
    assert all(
        contracts.latest_obligated_slot_at(binding_id=binding.binding_id) is None
        for binding in bindings
    )


def test_posterior_behavior_identity_includes_prior_behavior(base_app_config) -> None:
    preparation, *_rest = _preparation(base_app_config)
    original = preparation.prior_bindings[0]
    replacement = ForecastProducerBinding.create(
        contract_id=original.contract_id,
        producer_kind=original.producer_kind,
        producer_id=original.producer_id,
        producer_behavior_id="replacement-prior-behavior",
        permission=original.permission,
    )
    changed = replace(
        preparation,
        prior_bindings=(replacement, *preparation.prior_bindings[1:]),
    )

    assert changed.producer_behavior_id != preparation.producer_behavior_id


def test_posterior_behavior_identity_excludes_prior_capital_admission(
    base_app_config,
) -> None:
    preparation, *_rest = _preparation(base_app_config)
    capital_bindings = tuple(
        ForecastProducerBinding.create(
            contract_id=item.contract_id,
            producer_kind=item.producer_kind,
            producer_id=item.producer_id,
            producer_behavior_id=item.producer_behavior_id,
            permission=ForecastPermission.CAPITAL_CANDIDATE,
            required_feature_keys=item.required_feature_keys,
        )
        for item in preparation.prior_bindings
    )

    promoted = replace(preparation, prior_bindings=capital_bindings)

    assert promoted.producer_behavior_id == preparation.producer_behavior_id


def test_preparation_rejects_prior_from_an_unbound_behavior(base_app_config) -> None:
    preparation, _contracts, _forecasts, targets, _market, _engine = _preparation(base_app_config)
    wrong = targets[0].prior.model_copy(update={"producer_behavior_id": "unbound-prior-behavior"})

    with pytest.raises(ValueError, match="冻结 ProducerBinding"):
        preparation.reserve(
            (wrong, *(item.prior for item in targets[1:])),
            as_of=SLOT_AT + timedelta(minutes=2),
        )


def test_preparation_closes_reserved_obligations_when_world_model_fails(
    base_app_config,
) -> None:
    preparation, _contract_store, forecast_store, targets, _market, _engine = _preparation(
        base_app_config
    )
    seed = preparation.reserve(
        tuple(item.prior for item in targets),
        as_of=SLOT_AT + timedelta(minutes=2),
    )
    assert seed is not None
    preparation.close_seed(
        seed,
        attempted_at=SLOT_AT + timedelta(minutes=3),
        reason="PRODUCER_FAILED",
        detail="WORLD_MODEL_FAILED",
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
    preparation, contracts, forecasts, targets, market, engine = _preparation(base_app_config)
    seed = preparation.reserve(
        tuple(item.prior for item in targets),
        as_of=SLOT_AT + timedelta(minutes=2),
    )
    assert seed is not None
    packet = _packet()
    frozen = preparation.build_input(
        seed,
        world_model=_world_model(decision_packet_hash=packet.content_hash),
        packet=packet,
    )
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


def test_posterior_increment_is_scored_only_after_shared_outcomes_settle(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, engine, frozen, completed_at = _execution_fixture(
        base_app_config
    )
    application = ContextPosteriorApplication(
        analyst=_ExecutionAnalyst(
            preparation.producer_behavior_id,
            AnalystResult(
                True,
                _output(frozen, change=True, contribute=True),
                "CODEX_ANALYSIS_SUCCEEDED",
                attempts=1,
                completed_at=completed_at,
                run_id="posterior-evidence-run-1",
            ),
        ),
        contracts=contracts,
        forecasts=forecasts,
        market=market,
        maximum_quote_age_seconds=300,
        clock=lambda: SLOT_AT + timedelta(minutes=3),
    )
    application.execute(
        frozen,
        expected_behavior_hash=preparation.producer_behavior_id,
    )
    reader = SqlForecastIncrementEvidenceReader(
        engine=engine,
        outcome_evaluation_version="forecast-target-outcome-v1",
        candidate_producer_id="world-model-posterior",
        comparator_producer_id=PRIOR_PRODUCER_ID,
    )

    pending = reader.read()

    assert pending.status == ForecastIncrementStatus.AWAITING_SETTLEMENT
    assert pending.horizon_minutes == 4_320
    assert pending.due_panel_count == 1
    assert pending.forecast_panel_count == 1
    assert pending.pair.settled_panel_count == 0
    assert tuple(item.stratum.value for item in pending.source_evidence) == (
        "CADENCE_ONLY",
        "MATERIAL_STATE_ONLY",
    )
    assert pending.source_evidence[0].status == ForecastIncrementStatus.AWAITING_SETTLEMENT
    assert pending.source_evidence[0].forecast_panel_count == 1
    assert pending.source_evidence[1].status == ForecastIncrementStatus.NOT_STARTED

    for target in frozen.targets:
        gross_return = Decimal("100")
        realized = next(
            item.bucket_id
            for item in target.contract.outcome_buckets
            if (item.lower_bps is None or gross_return >= item.lower_bps)
            and (item.upper_bps is None or gross_return < item.upper_bps)
        )
        instrument = target.contract.target.legs[0].instrument
        forecasts.record_outcome(
            ForecastOutcome(
                outcome_id=stable_id(
                    "forecast_outcome",
                    target.slot.slot_id,
                    "forecast-target-outcome-v1",
                ),
                contract_id=target.contract.contract_id,
                decision_slot_id=target.slot.slot_id,
                evaluation_version="forecast-target-outcome-v1",
                status=ForecastOutcomeStatus.SETTLED,
                information_cutoff_at=target.slot.information_cutoff_at,
                evaluation_at=target.slot.evaluation_at,
                settled_at=target.slot.evaluation_at + timedelta(seconds=1),
                legs=(
                    ForecastLegOutcome(
                        instrument_id=instrument.key,
                        direction=target.contract.target.legs[0].direction,
                        gross_weight=Decimal("1"),
                        reference_price=Decimal("100"),
                        exit_price=Decimal("101"),
                        price_return_bps=gross_return,
                    ),
                ),
                gross_target_return_bps=gross_return,
                realized_bucket_id=realized,
                reason_code="GROSS_TARGET_RETURN_AVAILABLE",
            )
        )

    settled = reader.read()

    assert settled.status == ForecastIncrementStatus.EVIDENCE_AVAILABLE
    assert settled.pair.settled_panel_count == 1
    assert settled.pair.non_overlapping_panel_count == 1
    assert settled.pair.paired_target_count == 2
    assert settled.pair.mean_max_bucket_probability_delta == Decimal("0.01")
    assert settled.source_evidence[0].status == ForecastIncrementStatus.EVIDENCE_AVAILABLE
    assert settled.source_evidence[0].pair.settled_panel_count == 1


def test_increment_keeps_cadence_and_material_panels_separate_at_the_same_cutoff(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, engine, cadence, completed_at = _execution_fixture(
        base_app_config
    )
    analyst = _ExecutionAnalyst(
        preparation.producer_behavior_id,
        AnalystResult(
            True,
            _output(cadence, change=False, contribute=False),
            "CODEX_ANALYSIS_SUCCEEDED",
            attempts=1,
            completed_at=completed_at,
            run_id="posterior-source-strata-run",
        ),
    )
    application = ContextPosteriorApplication(
        analyst=analyst,
        contracts=contracts,
        forecasts=forecasts,
        market=market,
        maximum_quote_age_seconds=300,
        clock=lambda: completed_at,
    )
    application.execute(
        cadence,
        expected_behavior_hash=preparation.producer_behavior_id,
    )
    root = Path(__file__).resolve().parents[1]
    artifact = load_forecast_baseline(
        root / "evidence/forecast-baselines/forecast_baseline_7edf2cf090b47cdad2e5.json"
    )
    material_prior = RollingPriorForecastProducer(
        artifact=artifact,
        market=market,
        contracts=contracts,
        forecasts=forecasts,
        outcome_evaluation_version="forecast-target-outcome-v1",
        activated_at=SLOT_AT - timedelta(hours=1),
        maximum_quote_age_seconds=300,
        clock=lambda: SLOT_AT + timedelta(minutes=2),
    ).produce(
        as_of=SLOT_AT,
        cause=ForecastSlotCause.material_state(
            policy_version=MATERIAL_SLOT_POLICY_VERSION,
            trigger_refs=("same-cutoff-material-event",),
        ),
    )
    material_seed = preparation.reserve(
        material_prior,
        as_of=SLOT_AT + timedelta(minutes=3),
    )
    assert material_seed is not None
    packet = _packet()
    material = preparation.build_input(
        material_seed,
        world_model=_world_model(decision_packet_hash=packet.content_hash),
        packet=packet,
    )
    analyst.result = AnalystResult(
        True,
        _output(material, change=False, contribute=False),
        "CODEX_ANALYSIS_SUCCEEDED",
        attempts=1,
        completed_at=completed_at,
        run_id="posterior-source-strata-material-run",
    )
    application.execute(
        material,
        expected_behavior_hash=preparation.producer_behavior_id,
    )

    evidence = SqlForecastIncrementEvidenceReader(
        engine=engine,
        outcome_evaluation_version="forecast-target-outcome-v1",
        candidate_producer_id="world-model-posterior",
        comparator_producer_id=PRIOR_PRODUCER_ID,
    ).read()

    assert evidence.due_panel_count == 2
    assert tuple(item.due_panel_count for item in evidence.source_evidence) == (1, 1)
    assert tuple(item.forecast_panel_count for item in evidence.source_evidence) == (1, 1)


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


class _SuccessfulAssessmentApplication:
    def __init__(self, packet: DecisionPacket, completed_at: datetime) -> None:
        self.packet = packet
        self.completed_at = completed_at
        self.calls = 0

    def execute(self, command):
        self.calls += 1
        assert command.packet == self.packet
        assessment = _world_model(decision_packet_hash=self.packet.content_hash)
        return AssessmentExecution.create(
            status=AssessmentExecutionStatus.SUCCEEDED,
            packet_id=self.packet.packet_id,
            analysis_behavior_hash=command.analysis_behavior_hash,
            completed_at=self.completed_at,
            assessment=assessment,
            reason_code="CODEX_ANALYSIS_SUCCEEDED",
        )


class _FailedAssessmentApplication:
    def __init__(self, packet: DecisionPacket, completed_at: datetime) -> None:
        self.packet = packet
        self.completed_at = completed_at

    def execute(self, command):
        assert command.packet == self.packet
        return AssessmentExecution.create(
            status=AssessmentExecutionStatus.FAILED,
            packet_id=self.packet.packet_id,
            analysis_behavior_hash=command.analysis_behavior_hash,
            completed_at=self.completed_at,
            reason_code="CODEX_ACCOUNTS_UNAVAILABLE",
        )


class _ConflictingAssessmentApplication:
    def execute(self, _command):
        raise ValueError("authoritative conflict")


def test_world_model_update_is_published_after_posterior_terminal_state(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, _engine, frozen, completed_at = _execution_fixture(
        base_app_config
    )
    packet = _packet()
    published = []

    def publish(assessment):
        assert all(
            forecasts.result_for_behavior(
                decision_slot_id=target.slot.slot_id,
                producer_behavior_id=preparation.producer_behavior_id,
            )
            is not None
            for target in frozen.targets
        )
        published.append(assessment.assessment_id)

    application = ContextAssessmentPosteriorApplication(
        assessment=_SuccessfulAssessmentApplication(packet, completed_at),
        preparation=preparation,
        posterior=ContextPosteriorApplication(
            analyst=_ExecutionAnalyst(
                preparation.producer_behavior_id,
                AnalystResult(
                    True,
                    _output(frozen, change=False, contribute=False),
                    "CODEX_ANALYSIS_SUCCEEDED",
                    completed_at=completed_at,
                ),
            ),
            contracts=contracts,
            forecasts=forecasts,
            market=market,
            maximum_quote_age_seconds=300,
            clock=lambda: SLOT_AT + timedelta(minutes=3),
        ),
        on_world_model_complete=publish,
    )

    result = application.execute(
        command=AssessmentCommand.create(
            packet=packet,
            analysis_behavior_hash="a" * 64,
        ),
        seed=ContextPosteriorSeed.create(
            information_cutoff_at=SLOT_AT,
            targets=frozen.targets,
        ),
        expected_behavior_hash=preparation.producer_behavior_id,
    )

    assert result.status == PosteriorExecutionStatus.SUCCEEDED
    assert published == [frozen.world_model.assessment_id]


def test_same_cutoff_world_model_failure_closes_posterior_obligations(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, _engine, frozen, completed_at = _execution_fixture(
        base_app_config
    )
    packet = _packet()
    posterior = ContextPosteriorApplication(
        analyst=_ExecutionAnalyst(
            preparation.producer_behavior_id,
            AnalystResult(False, None, "SHOULD_NOT_RUN"),
        ),
        contracts=contracts,
        forecasts=forecasts,
        market=market,
        maximum_quote_age_seconds=300,
    )
    application = ContextAssessmentPosteriorApplication(
        assessment=_FailedAssessmentApplication(packet, completed_at),
        preparation=preparation,
        posterior=posterior,
    )
    seed = ContextPosteriorSeed.create(
        information_cutoff_at=SLOT_AT,
        targets=frozen.targets,
    )

    result = application.execute(
        command=AssessmentCommand.create(
            packet=packet,
            analysis_behavior_hash="a" * 64,
        ),
        seed=seed,
        expected_behavior_hash=preparation.producer_behavior_id,
    )

    assert result.status == PosteriorExecutionStatus.NO_ESTIMATE
    assert len(result.no_estimate_ids) == len(seed.targets)
    assert result.reason_code == "WORLD_MODEL_CODEX_ACCOUNTS_UNAVAILABLE"


def test_orchestration_failure_closes_pre_registered_posterior_obligations(
    base_app_config,
) -> None:
    preparation, contracts, forecasts, market, _engine, frozen, _completed_at = _execution_fixture(
        base_app_config
    )
    analyst = _ExecutionAnalyst(
        preparation.producer_behavior_id,
        AnalystResult(False, None, "SHOULD_NOT_RUN"),
    )
    seed = ContextPosteriorSeed.create(
        information_cutoff_at=SLOT_AT,
        targets=frozen.targets,
    )
    application = ContextAssessmentPosteriorApplication(
        assessment=_SuccessfulAssessmentApplication(
            _packet(),
            SLOT_AT + timedelta(minutes=5),
        ),
        preparation=preparation,
        posterior=ContextPosteriorApplication(
            analyst=analyst,
            contracts=contracts,
            forecasts=forecasts,
            market=market,
            maximum_quote_age_seconds=300,
        ),
    )

    result = application.close_orchestration_failure(
        seed=seed,
        expected_behavior_hash=preparation.producer_behavior_id,
        completed_at=SLOT_AT + timedelta(hours=1, minutes=1),
        reason_code="POSTERIOR_ACTIVITY_FAILED",
    )

    assert result.status == PosteriorExecutionStatus.NO_ESTIMATE
    assert result.reason_code == "POSTERIOR_ACTIVITY_FAILED"
    assert len(result.no_estimate_ids) == len(seed.targets)
    assert analyst.calls == 0


def test_posterior_workflow_terminalizes_obligations_after_activity_failure(
    base_app_config,
) -> None:
    async def scenario() -> None:
        preparation, contracts, forecasts, market, _engine, frozen, _completed_at = (
            _execution_fixture(base_app_config)
        )
        analyst = _ExecutionAnalyst(
            preparation.producer_behavior_id,
            AnalystResult(False, None, "SHOULD_NOT_RUN"),
        )
        combined = ContextAssessmentPosteriorApplication(
            assessment=_ConflictingAssessmentApplication(),
            preparation=preparation,
            posterior=ContextPosteriorApplication(
                analyst=analyst,
                contracts=contracts,
                forecasts=forecasts,
                market=market,
                maximum_quote_age_seconds=300,
            ),
        )
        policy = base_app_config.temporal.model_copy(
            update={"assessment_task_queue": "posterior-failure-terminal-test"}
        )
        packet = _packet()
        seed = ContextPosteriorSeed.create(
            information_cutoff_at=SLOT_AT,
            targets=frozen.targets,
        )
        request = PosteriorWorkflowRequest.create(
            seed=seed,
            assessment_command=AssessmentCommand.create(
                packet=packet,
                analysis_behavior_hash="a" * 64,
            ),
            producer_behavior_id=preparation.producer_behavior_id,
            orchestration=OrchestrationPolicySnapshot.from_config(policy),
            created_at=SLOT_AT + timedelta(minutes=2),
        )
        async with (
            await WorkflowEnvironment.start_time_skipping() as environment,
            AssessmentTemporalWorker(
                environment.client,
                policy,
                _ConflictingAssessmentApplication(),
                posterior_application=combined,
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
        assert result.status == PosteriorWorkflowStatus.NO_ESTIMATE
        assert result.reason_code == "POSTERIOR_ACTIVITY_FAILED"
        assert result.execution is not None
        assert len(result.execution.no_estimate_ids) == len(seed.targets)
        assert analyst.calls == 0

    asyncio.run(scenario())


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
        packet = _packet()
        seed = ContextPosteriorSeed.create(
            information_cutoff_at=SLOT_AT,
            targets=frozen.targets,
        )
        assessment_application = _SuccessfulAssessmentApplication(packet, completed_at)
        combined = ContextAssessmentPosteriorApplication(
            assessment=assessment_application,
            preparation=preparation,
            posterior=application,
        )
        request = PosteriorWorkflowRequest.create(
            seed=seed,
            assessment_command=AssessmentCommand.create(
                packet=packet,
                analysis_behavior_hash="a" * 64,
            ),
            producer_behavior_id=preparation.producer_behavior_id,
            orchestration=OrchestrationPolicySnapshot.from_config(policy),
            created_at=SLOT_AT + timedelta(minutes=2),
        )
        async with (
            await WorkflowEnvironment.start_time_skipping() as environment,
            AssessmentTemporalWorker(
                environment.client,
                policy,
                assessment_application,
                posterior_application=combined,
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
        assert assessment_application.calls == 1

    asyncio.run(scenario())


def _posterior_trigger_config_and_batch(app_config):
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
        trigger_type=AnalysisTriggerType.HEARTBEAT,
        symbol=config.assessment.review_trigger_symbol,
        pipeline_id=config.pipeline.version,
        occurred_at=SLOT_AT + timedelta(minutes=1),
        observed_at=SLOT_AT + timedelta(minutes=1),
        priority=1,
        dedup_key="posterior-heartbeat-v1",
    )
    return config, build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=SLOT_AT + timedelta(minutes=1),
        deadline=SLOT_AT + timedelta(minutes=15),
    )


def test_heartbeat_dispatches_one_due_joint_posterior_without_standalone_assessment(
    app_config,
) -> None:
    contracts, forecasts, _market, engine, prior, targets = _prior_runtime()
    config, batch = _posterior_trigger_config_and_batch(app_config)
    preparation = ContextPosteriorPreparation(
        contracts=tuple(item.contract for item in targets),
        prior_bindings=tuple(item.binding for item in targets),
        runtime=config.codex_runtime,
        world_model_behavior_id=configured_assess_behavior_hash(config),
        activated_at=SLOT_AT - timedelta(hours=1),
        contract_store=contracts,
        forecast_store=forecasts,
    )
    command = AssessmentCommand.create(
        packet=_packet(),
        analysis_behavior_hash="a" * 64,
    )
    analysis_identities = []
    required_reviews = []

    class DispatchBuilder(TriggerDispatchBuilder):
        def _assessment_command(
            self,
            batch,
            *,
            as_of,
            analysis_identity,
            required_review_requests=(),
        ):
            assert as_of == SLOT_AT
            analysis_identities.append(analysis_identity)
            required_reviews.extend(required_review_requests)
            return command

    dispatches = DispatchBuilder(
        config=config,
        program_forecast_producers=(prior,),
        posterior_preparation=preparation,
    ).build(batch)

    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert dispatch.workflow_name == "ContextPosteriorWorkflow"
    request = PosteriorWorkflowRequest.model_validate(dispatch.payload)
    assert request.seed.information_cutoff_at == SLOT_AT
    assert len(request.seed.targets) == 2
    assert all(item.prior.producer_id == PRIOR_PRODUCER_ID for item in request.seed.targets)
    assert analysis_identities == [request.seed.seed_id]
    assert len(required_reviews) == 1
    assert required_reviews[0].requested_at == SLOT_AT
    assert required_reviews[0].evidence_ids == ()
    assert "Forecast DecisionSlot" in required_reviews[0].reason
    assert request.assessment_command == command
    assert request.producer_behavior_id == preparation.producer_behavior_id
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(forecast_producer_bindings)
        ).scalar_one() == 4
        assert connection.execute(
            select(func.count()).select_from(forecast_slot_obligations)
        ).scalar_one() == 4
        assert connection.execute(select(func.count()).select_from(forecast_rows)).scalar_one() == 2


def test_trigger_closes_reserved_posterior_when_world_packet_is_unavailable(
    app_config,
) -> None:
    frozen = _input()
    seed = ContextPosteriorSeed.create(
        information_cutoff_at=SLOT_AT,
        targets=frozen.targets,
    )
    closed = []

    class PriorProducer:
        def produce(self, *, as_of):
            assert as_of == SLOT_AT + timedelta(minutes=1)
            return tuple(item.prior for item in frozen.targets)

    class PosteriorPreparation:
        producer_behavior_id = "posterior-behavior-v1"

        def activate(self):
            return ()

        def reserve(self, prior_results, *, as_of):
            assert prior_results == tuple(item.prior for item in frozen.targets)
            assert as_of == SLOT_AT + timedelta(minutes=1)
            return seed

        def close_seed(self, value, *, attempted_at, reason, detail):
            closed.append((value, attempted_at, reason, detail))

    class DispatchBuilder(TriggerDispatchBuilder):
        def _assessment_command(
            self,
            batch,
            *,
            as_of,
            analysis_identity,
            required_review_requests=(),
        ):
            assert as_of == SLOT_AT
            assert analysis_identity == seed.seed_id
            assert len(required_review_requests) == 1
            assert required_review_requests[0].requested_at == SLOT_AT
            return None

    config, batch = _posterior_trigger_config_and_batch(app_config)

    dispatches = DispatchBuilder(
        config=config,
        program_forecast_producers=(PriorProducer(),),
        posterior_preparation=PosteriorPreparation(),
    ).build(batch)

    assert dispatches == ()
    assert closed == [
        (
            seed,
            SLOT_AT + timedelta(minutes=1),
            ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
            "WORLD_MODEL_DECISION_PACKET_UNAVAILABLE",
        )
    ]
