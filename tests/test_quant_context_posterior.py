import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.entrypoints.dashboard.evaluation import EvaluationDashboardReader
from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.estimate import (
    ContextForecastDraft,
    ContextForecastStructuredOutput,
    QuantContextPosteriorDraft,
)
from investment_manager.forecast.context.posterior import (
    QuantContextPosteriorPreallocator,
    QuantContextPosteriorRunner,
    QuantContextPosteriorTarget,
    SqlQuantContextPosteriorRepository,
    audit_quant_context_posterior_draft,
    build_quant_context_posterior_assignment,
    quant_context_posterior_behavior_id,
)
from investment_manager.forecast.context.producer import context_forecast_contract
from investment_manager.forecast.context.stability import (
    SqlContextForecastStabilityRepository,
)
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ExposureDirection
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.market.models import MarketQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

NOW = datetime(2026, 8, 28, 3, tzinfo=UTC)


class _PosteriorAnalyst:
    def __init__(self, completed_at: datetime) -> None:
        self.completed_at = completed_at
        self.calls = 0

    def estimate(self, assignment) -> AnalystResult:
        self.calls += 1
        target = assignment.analysis_targets[0]
        return AnalystResult(
            success=True,
            output=ContextForecastStructuredOutput.model_validate(
                {
                    "forecasts": [
                        {
                            "decision_slot_id": target.slot.slot_id,
                            "outcome_probabilities": [
                                {"bucket_id": "EXTREME_LOSS", "probability": "0.04"},
                                {"bucket_id": "LARGE_LOSS", "probability": "0.08"},
                                {"bucket_id": "LOSS", "probability": "0.12"},
                                {"bucket_id": "SMALL_LOSS", "probability": "0.13"},
                                {"bucket_id": "NEUTRAL", "probability": "0.13"},
                                {"bucket_id": "SMALL_GAIN", "probability": "0.16"},
                                {"bucket_id": "GAIN", "probability": "0.16"},
                                {"bucket_id": "LARGE_GAIN", "probability": "0.11"},
                                {"bucket_id": "EXTREME_GAIN", "probability": "0.07"},
                            ],
                            "mechanism_contributions": [
                                {
                                    "mechanism_id": "mechanism-1",
                                    "effect": "UPSIDE",
                                    "rationale": "资金传导相对量化先验增加了可证伪的上行概率。",
                                }
                            ],
                            "evidence_refs": ["evidence-1"],
                            "invalidation_conditions": ["现货资金流转为持续净流出"],
                        }
                    ]
                }
            ),
            reason_code="CODEX_OK",
            completed_at=self.completed_at,
        )


def _fixture():
    config = load_config("config/investment-manager.shadow.yaml")
    context = config.capital.context_forecast
    posterior = config.outcome_evaluation.quant_context_posterior
    quant = config.outcome_evaluation.quant_baseline
    assert context is not None and posterior is not None and quant is not None
    target_policy = context.targets[0]
    instrument = next(
        item
        for item in config.capital.forecast_reference_instruments
        if item.key == target_policy.reference_instrument_key
    )
    contract = context_forecast_contract(
        policy=context,
        target_policy=target_policy,
        instrument=instrument,
        cost_semantics_version=config.capital.decision.cost_model_version,
    )
    formal_binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id=context.producer_id,
        producer_behavior_id=context.producer_behavior_id,
        permission=ForecastPermission.CAPITAL_CANDIDATE,
        required_feature_keys=target_policy.required_feature_keys,
    )
    quant_behavior = stable_id("quant_forecast_behavior", "test")
    posterior_behavior = quant_context_posterior_behavior_id(
        config=config,
        contracts=(contract,),
        quant_producer_behavior_id=quant_behavior,
    )
    posterior_binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id=posterior.producer_id,
        producer_behavior_id=posterior_behavior,
        permission=ForecastPermission.RESEARCH,
    )
    cutoff = ForecastPriceAnchor(
        instrument_id=instrument.key,
        price=Decimal("80000"),
        observed_at=NOW,
        available_at=NOW,
        quote_ref="cutoff-quote",
    )
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=(cutoff,),
    )
    probabilities = tuple(
        ForecastBucketProbability(
            bucket_id=item.bucket_id,
            probability=probability,
        )
        for item, probability in zip(
            contract.outcome_buckets,
            tuple(item.probability for item in contract.forecast_benchmark),
            strict=True,
        )
    )
    panel = {
        "purpose": "PROGRAM_QUANT_FORECAST",
        "panel_version": "quant-reliability-panel-v5",
        "artifact_id": "artifact-1",
        "inference_version": "conditional-empirical-dirichlet-v1",
        "decision_slot_id": slot.slot_id,
        "features": {
            "observed_at": NOW,
            "return_60m_bps": "10",
            "return_240m_bps": "30",
            "volatility_240m_bps": "8",
        },
        "quant_prior": {
            "model_name": "momentum_reversal_volatility",
            "outcome_probabilities": probabilities,
        },
        "candidate_predictions": (),
        "maximum_bucket_probability_range": "0.04",
    }
    quant_forecast = BaseForecast(
        forecast_id=stable_id("base_forecast", slot.slot_id, quant_behavior),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        producer_id=quant.producer_id,
        producer_behavior_id=quant_behavior,
        outcome_family_id=contract.outcome_family_id,
        target=contract.target,
        horizon_minutes=contract.horizon_minutes,
        cutoff_prices=slot.cutoff_prices,
        entry_prices=(cutoff.model_copy(update={"available_at": NOW + timedelta(seconds=1)}),),
        information_cutoff_at=NOW,
        input_observed_at=NOW,
        available_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(minutes=60),
        outcome_probabilities=probabilities,
        expected_gross_bps=sum(
            (
                probability.probability * bucket.representative_bps
                for probability, bucket in zip(
                    probabilities,
                    contract.outcome_buckets,
                    strict=True,
                )
            ),
            Decimal("0"),
        ),
        input_refs=("artifact-1", "cutoff-quote"),
        program_input_json=canonical_json(panel),
        program_input_hash=content_hash(panel),
    )
    formal_input = {
        "purpose": "FORECAST_ESTIMATE",
        "forecast_targets": (
            {
                "decision_slot": {
                    "decision_slot_id": slot.slot_id,
                    "information_cutoff_at": slot.information_cutoff_at,
                    "completion_deadline_at": slot.completion_deadline_at,
                    "evaluation_at": slot.evaluation_at,
                    "cause_origin": "CADENCE",
                },
                "forecast_contract": {
                    "contract_id": contract.contract_id,
                    "outcome_family_id": contract.outcome_family_id,
                    "horizon_minutes": contract.horizon_minutes,
                    "reference_instrument": {
                        "symbol": instrument.symbol,
                        "product": instrument.product.value,
                    },
                    "outcome_buckets": tuple(
                        {
                            "bucket_id": item.bucket_id,
                            "lower_bps": item.lower_bps,
                            "upper_bps": item.upper_bps,
                            "representative_bps": item.representative_bps,
                        }
                        for item in contract.outcome_buckets
                    ),
                    "forecast_benchmark": tuple(
                        {
                            "bucket_id": item.bucket_id,
                            "probability": item.probability,
                        }
                        for item in contract.forecast_benchmark
                    ),
                },
                "target_state": {
                    "as_of": NOW,
                    "asset_states": (),
                    "derivative_states": (),
                    "comparison_states": (),
                    "missing_comparison_instrument_keys": (),
                },
            },
        ),
        "world_model": {
            "assessment_id": "assessment-1",
            "as_of": NOW,
            "available_at": NOW,
            "synthesis": "流动性支持仍在传导。",
            "synthesis_horizon_hours": 24,
            "event_references": (
                {
                    "evidence_id": "evidence-1",
                    "source": "OFFICIAL",
                    "title": "资金事实",
                    "event_time": NOW,
                    "impact_state": "CURRENT",
                },
            ),
            "mechanisms": (
                {
                    "mechanism_id": "mechanism-1",
                    "relationship": "SUPPORTS",
                    "claim": "流动性支持风险资产。",
                    "horizon_hours": 24,
                    "transmission_stage": "PROPAGATING",
                    "evidence_ids": ("evidence-1",),
                    "conflicting_evidence_ids": (),
                },
            ),
        },
    }
    target = QuantContextPosteriorTarget(
        slot=slot,
        contract=contract,
        binding=posterior_binding,
        instrument=instrument,
        input_observed_at=NOW,
        quant_forecast_id=quant_forecast.forecast_id,
        quant_input_refs=("artifact-1", quant_forecast.forecast_id),
    )
    assignment = build_quant_context_posterior_assignment(
        policy=posterior,
        producer_behavior_id=posterior_behavior,
        formal_producer_behavior_id=context.producer_behavior_id,
        quant_producer_behavior_id=quant_behavior,
        targets=(target,),
        formal_analysis_input=formal_input,
        quant_forecasts={slot.slot_id: quant_forecast},
        assigned_at=NOW + timedelta(seconds=2),
    )
    return config, contract, formal_binding, posterior_binding, quant_forecast, assignment


def test_quant_context_posterior_behavior_is_independent_of_contract_order() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    context = config.capital.context_forecast
    assert context is not None
    instruments = {item.key: item for item in config.capital.forecast_reference_instruments}
    instruments.update(
        {item.instrument.key: item.instrument for item in config.capital.execution_specs}
    )
    contracts = tuple(
        context_forecast_contract(
            policy=context,
            target_policy=target,
            instrument=instruments[target.reference_instrument_key],
            cost_semantics_version=config.capital.decision.cost_model_version,
        )
        for target in context.targets
    )

    forward = quant_context_posterior_behavior_id(
        config=config,
        contracts=contracts,
        quant_producer_behavior_id="a" * 64,
    )
    reverse = quant_context_posterior_behavior_id(
        config=config,
        contracts=tuple(reversed(contracts)),
        quant_producer_behavior_id="a" * 64,
    )

    assert forward == reverse


def test_quant_context_posterior_exposes_selected_prior_not_candidate_distributions() -> None:
    *_, quant_forecast, assignment = _fixture()

    program_panel = json.loads(quant_forecast.program_input_json or "{}")
    model_input = json.loads(assignment.analysis_input_json)
    projected_panel = model_input["forecast_targets"][0]["quant_panel"]

    assert "candidate_predictions" in program_panel
    assert "candidate_predictions" not in projected_panel
    assert projected_panel["features"] == program_panel["features"]
    assert projected_panel["quant_prior"] == program_panel["quant_prior"]
    assert projected_panel["maximum_bucket_probability_range"] == "0.04"


def test_posterior_no_material_effect_uses_exact_quant_prior() -> None:
    *_, quant_forecast, assignment = _fixture()
    model_input = json.loads(assignment.analysis_input_json)
    draft = QuantContextPosteriorDraft.model_validate(
        {
            "decision_slot_id": quant_forecast.decision_slot_id,
            "outcome_probabilities": [
                {"bucket_id": item.bucket_id, "probability": probability}
                for item, probability in zip(
                    quant_forecast.outcome_probabilities,
                    ("0.06", "0.10", "0.14", "0.14", "0.10", "0.14", "0.14", "0.09", "0.09"),
                    strict=True,
                )
            ],
            "mechanism_contributions": [
                {
                    "mechanism_id": "mechanism-1",
                    "effect": "NO_MATERIAL_EFFECT",
                    "rationale": "当前机制不足以改变历史条件分布。",
                }
            ],
            "evidence_refs": [],
            "invalidation_conditions": ["资金事实发生可观察反转"],
        }
    )

    audited = audit_quant_context_posterior_draft(
        draft=draft,
        quant_prior=quant_forecast,
        analysis_input=model_input,
    )

    assert tuple(Decimal(item.probability) for item in audited.outcome_probabilities) == tuple(
        item.probability for item in quant_forecast.outcome_probabilities
    )
    assert audited.evidence_refs == ()


def test_posterior_substantive_adjustment_requires_mechanism_evidence() -> None:
    *_, quant_forecast, assignment = _fixture()
    model_input = json.loads(assignment.analysis_input_json)
    model_input["world_model"]["event_references"].append(
        {
            "evidence_id": "unrelated-evidence",
            "source": "OFFICIAL",
            "title": "无关事实",
            "event_time": NOW.isoformat(),
            "impact_state": "CURRENT",
        }
    )
    draft = _PosteriorAnalyst(NOW).estimate(assignment).output.forecasts[0].model_copy(
        update={"evidence_refs": ("unrelated-evidence",)}
    )

    try:
        audit_quant_context_posterior_draft(
            draft=draft,
            quant_prior=quant_forecast,
            analysis_input=model_input,
        )
    except ValueError as exc:
        assert "对应 mechanism" in str(exc)
    else:
        raise AssertionError("未绑定机制证据的 posterior 调整必须被拒绝")


def test_posterior_substantive_claim_must_change_quant_prior() -> None:
    *_, quant_forecast, assignment = _fixture()
    model_input = json.loads(assignment.analysis_input_json)
    original = _PosteriorAnalyst(NOW).estimate(assignment).output.forecasts[0]
    payload = original.model_dump(mode="json")
    payload["outcome_probabilities"] = [
        {
            "bucket_id": item.bucket_id,
            "probability": format(item.probability, "f"),
        }
        for item in quant_forecast.outcome_probabilities
    ]
    draft = ContextForecastDraft.model_validate(payload)

    try:
        audit_quant_context_posterior_draft(
            draft=draft,
            quant_prior=quant_forecast,
            analysis_input=model_input,
        )
    except ValueError as exc:
        assert "未改变 Quant prior" in str(exc)
    else:
        raise AssertionError("未改变分布的实质影响声明必须被拒绝")


def test_quant_context_posterior_uses_common_forecast_and_outcome_ledger() -> None:
    (
        config,
        contract,
        formal_binding,
        posterior_binding,
        quant_forecast,
        assignment,
    ) = _fixture()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contracts = SqlForecastContractStore(engine)
    contracts.record_contract(contract)
    contracts.record_binding(formal_binding, activated_at=NOW)
    contracts.record_binding(posterior_binding, activated_at=NOW)
    contracts.record_slot(assignment.targets[0].slot, binding=formal_binding)
    quant_binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id=quant_forecast.producer_id,
        producer_behavior_id=quant_forecast.producer_behavior_id,
        permission=ForecastPermission.RESEARCH,
    )
    contracts.record_binding(quant_binding, activated_at=NOW)
    contracts.record_obligation(slot=assignment.targets[0].slot, binding=quant_binding)
    SqlForecastStore(engine).record(quant_forecast)
    repository = SqlQuantContextPosteriorRepository(engine)

    assert repository.record_assignment(assignment)
    assert not repository.record_assignment(assignment)
    assert (
        contracts.obligation(
            stable_id(
                "forecast_slot_obligation",
                assignment.targets[0].slot.slot_id,
                posterior_binding.binding_id,
            )
        )
        is not None
    )

    completed_at = NOW + timedelta(seconds=10)
    SqlMarketDataStore(engine).put_quote(
        MarketQuote(
            quote_id="posterior-entry-quote",
            symbol="BTCUSDT",
            observed_at=completed_at,
            bid=Decimal("80009"),
            bid_quantity=Decimal("10"),
            ask=Decimal("80010"),
            ask_quantity=Decimal("10"),
            update_id=1,
            source="test",
        )
    )
    posterior_policy = config.outcome_evaluation.quant_context_posterior
    stability_policy = config.outcome_evaluation.context_forecast_stability
    assert posterior_policy is not None and stability_policy is not None
    stability_repository = SqlContextForecastStabilityRepository(engine)

    class PreregisteredAnalyst(_PosteriorAnalyst):
        def estimate(self, frozen_assignment):
            preregistered = stability_repository.assignments(
                policy_version=stability_policy.version,
                formal_producer_behavior_id=frozen_assignment.producer_behavior_id,
            )
            assert len(preregistered) == 1
            assert (
                preregistered[0].formal_analysis_input_json
                == frozen_assignment.analysis_input_json
            )
            assert preregistered[0].formal_prompt == frozen_assignment.prompt
            assert (
                preregistered[0].formal_output_schema_json
                == frozen_assignment.output_schema_json
            )
            return super().estimate(frozen_assignment)

    analyst = PreregisteredAnalyst(completed_at)
    runner = QuantContextPosteriorRunner(
        policy=posterior_policy,
        producer_behavior_id=assignment.producer_behavior_id,
        repository=repository,
        contracts=contracts,
        forecasts=SqlForecastStore(engine),
        market=SqlMarketDataStore(engine),
        analyst=analyst,
        maximum_quote_age_seconds=300,
        stability_policy=stability_policy,
        stability_repository=stability_repository,
    )

    report = runner.reconcile(as_of=NOW + timedelta(seconds=3))
    replay = runner.reconcile(as_of=NOW + timedelta(seconds=20))

    assert report.forecast_count == replay.forecast_count == 1
    assert report.no_estimate_count == replay.no_estimate_count == 0
    assert report.pending_count == replay.pending_count == 0
    assert analyst.calls == 1
    assert len(
        stability_repository.assignments(
            policy_version=stability_policy.version,
            formal_producer_behavior_id=assignment.producer_behavior_id,
        )
    ) == 1
    forecast = SqlForecastStore(engine).result_for_behavior(
        decision_slot_id=assignment.targets[0].slot.slot_id,
        producer_behavior_id=assignment.producer_behavior_id,
    )
    assert isinstance(forecast, BaseForecast)
    assert forecast.world_model_id == "assessment-1"
    assert forecast.analysis_input_json == assignment.analysis_input_json
    assert forecast.producer_id == posterior_policy.producer_id
    SqlForecastStore(engine).record_outcome(
        ForecastOutcome(
            outcome_id=stable_id(
                "forecast_outcome",
                assignment.targets[0].slot.slot_id,
                config.outcome_evaluation.target_forecast_version,
            ),
            contract_id=contract.contract_id,
            decision_slot_id=assignment.targets[0].slot.slot_id,
            evaluation_version=config.outcome_evaluation.target_forecast_version,
            status=ForecastOutcomeStatus.SETTLED,
            information_cutoff_at=assignment.information_cutoff_at,
            outcome_start_at=assignment.targets[0].slot.outcome_start_at,
            evaluation_at=assignment.evaluation_at,
            settled_at=assignment.evaluation_at + timedelta(seconds=1),
            legs=(
                ForecastLegOutcome(
                    instrument_id=contract.target.legs[0].instrument.key,
                    direction=ExposureDirection.LONG,
                    gross_weight=Decimal("1"),
                    reference_price=Decimal("80000"),
                    exit_price=Decimal("80800"),
                    price_return_bps=Decimal("100"),
                ),
            ),
            gross_target_return_bps=Decimal("100"),
            realized_bucket_id="LARGE_GAIN",
            reason_code="SETTLED",
        )
    )
    reader = EvaluationDashboardReader(engine, config)
    with engine.connect() as connection:
        pair = reader._forecast_pair_evidence(
            connection,
            contracts=(contract,),
            candidate_producer_id=posterior_policy.producer_id,
            candidate_behavior_id=assignment.producer_behavior_id,
            comparator_producer_id=quant_forecast.producer_id,
            comparator_behavior_id=quant_forecast.producer_behavior_id,
        )

    assert pair is not None
    assert pair.settled_panel_count == 1
    assert pair.paired_target_count == 1
    assert pair.mean_candidate_brier_score == Decimal("0.9044")
    assert pair.mean_comparator_brier_score == Decimal("0.9250")
    assert pair.mean_brier_improvement == Decimal("0.0206")
    assert pair.mean_max_bucket_probability_delta == Decimal("0.03")


def test_quant_context_posterior_records_missing_prior_without_calling_ai() -> None:
    config, contract, formal_binding, posterior_binding, _, assignment = _fixture()
    posterior_policy = config.outcome_evaluation.quant_context_posterior
    assert posterior_policy is not None
    posterior_policy = posterior_policy.model_copy(update={"activated_at": NOW})
    quant_no_estimate_id = stable_id(
        "forecast_no_estimate",
        assignment.targets[0].slot.slot_id,
        assignment.quant_producer_behavior_id,
    )
    target = assignment.targets[0].model_copy(
        update={
            "quant_forecast_id": None,
            "quant_no_estimate_id": quant_no_estimate_id,
            "quant_reason": ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
            "quant_input_refs": (quant_no_estimate_id,),
        }
    )
    missing_assignment = build_quant_context_posterior_assignment(
        policy=posterior_policy,
        producer_behavior_id=assignment.producer_behavior_id,
        formal_producer_behavior_id=assignment.formal_producer_behavior_id,
        quant_producer_behavior_id=assignment.quant_producer_behavior_id,
        targets=(target,),
        formal_analysis_input=json.loads(assignment.analysis_input_json),
        quant_forecasts={},
        assigned_at=assignment.assigned_at,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contracts = SqlForecastContractStore(engine)
    contracts.record_contract(contract)
    contracts.record_binding(formal_binding, activated_at=NOW)
    contracts.record_binding(posterior_binding, activated_at=NOW)
    contracts.record_slot(target.slot, binding=formal_binding)
    quant_binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="program-quant-baseline",
        producer_behavior_id=missing_assignment.quant_producer_behavior_id,
        permission=ForecastPermission.RESEARCH,
    )
    contracts.record_binding(quant_binding, activated_at=NOW)
    contracts.record_obligation(slot=target.slot, binding=quant_binding)
    contracts.record_no_estimate(
        ForecastNoEstimate(
            result_id=quant_no_estimate_id,
            slot_id=target.slot.slot_id,
            contract_id=target.contract.contract_id,
            producer_kind=ForecastProducerKind.PROGRAM,
            producer_id="program-quant-baseline",
            producer_behavior_id=missing_assignment.quant_producer_behavior_id,
            reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
            information_cutoff_at=target.slot.information_cutoff_at,
            attempted_at=target.slot.slot_as_of,
            completed_at=target.slot.slot_as_of,
            input_refs=(quant_no_estimate_id,),
        )
    )
    repository = SqlQuantContextPosteriorRepository(engine)
    repository.record_assignment(missing_assignment)
    analyst = _PosteriorAnalyst(NOW + timedelta(seconds=10))
    runner = QuantContextPosteriorRunner(
        policy=posterior_policy,
        producer_behavior_id=missing_assignment.producer_behavior_id,
        repository=repository,
        contracts=contracts,
        forecasts=SqlForecastStore(engine),
        market=SqlMarketDataStore(engine),
        analyst=analyst,
        maximum_quote_age_seconds=300,
    )

    report = runner.reconcile(as_of=NOW + timedelta(seconds=3))

    assert report.forecast_count == 0
    assert report.no_estimate_count == 1
    assert report.pending_count == 0
    assert analyst.calls == 0


def test_quant_context_posterior_records_formal_target_absence_on_shared_slot() -> None:
    (
        config,
        contract,
        formal_binding,
        posterior_binding,
        quant_forecast,
        assignment,
    ) = _fixture()
    posterior_policy = config.outcome_evaluation.quant_context_posterior
    assert posterior_policy is not None
    posterior_policy = posterior_policy.model_copy(update={"activated_at": NOW})
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contracts = SqlForecastContractStore(engine)
    forecasts = SqlForecastStore(engine)
    contracts.record_contract(contract)
    contracts.record_binding(formal_binding, activated_at=NOW)
    contracts.record_binding(posterior_binding, activated_at=NOW)
    slot = assignment.targets[0].slot
    contracts.record_slot(slot, binding=formal_binding)
    quant_binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id=quant_forecast.producer_id,
        producer_behavior_id=quant_forecast.producer_behavior_id,
        permission=ForecastPermission.RESEARCH,
    )
    contracts.record_binding(quant_binding, activated_at=NOW)
    contracts.record_obligation(slot=slot, binding=quant_binding)
    forecasts.record(quant_forecast)
    formal_absence = ForecastNoEstimate(
        result_id=stable_id(
            "forecast_no_estimate",
            slot.slot_id,
            formal_binding.producer_behavior_id,
        ),
        slot_id=slot.slot_id,
        contract_id=contract.contract_id,
        producer_kind=formal_binding.producer_kind,
        producer_id=formal_binding.producer_id,
        producer_behavior_id=formal_binding.producer_behavior_id,
        reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
        information_cutoff_at=slot.information_cutoff_at,
        attempted_at=slot.slot_as_of,
        completed_at=slot.slot_as_of,
        input_refs=("formal-target-state-missing",),
        detail="TARGET_STATE_UNAVAILABLE:ValueError",
    )
    contracts.record_no_estimate(formal_absence)
    repository = SqlQuantContextPosteriorRepository(engine)
    preallocator = QuantContextPosteriorPreallocator(
        policy=posterior_policy,
        formal_producer_behavior_id=formal_binding.producer_behavior_id,
        quant_producer_behavior_id=quant_binding.producer_behavior_id,
        producer_behavior_id=posterior_binding.producer_behavior_id,
        bindings_by_contract={contract.contract_id: posterior_binding},
        contracts=contracts,
        forecasts=forecasts,
        repository=repository,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    preallocator.before_estimate(
        slot=slot,
        formal_producer_behavior_id=formal_binding.producer_behavior_id,
        formal_analysis_input=None,
        formal_output_schema=None,
    )

    posterior_absence = contracts.no_estimate(
        stable_id(
            "forecast_no_estimate",
            slot.slot_id,
            posterior_binding.producer_behavior_id,
        )
    )
    assert posterior_absence is not None
    assert posterior_absence.reason == ForecastNoEstimateReason.MARKET_INPUT_INVALID
    assert posterior_absence.detail == "FORMAL_INPUT_UNAVAILABLE:MARKET_INPUT_INVALID"
    assert formal_absence.result_id in posterior_absence.input_refs
    assert quant_forecast.forecast_id in posterior_absence.input_refs
    assert repository.assignments(
        policy_version=posterior_policy.version,
        producer_behavior_id=posterior_binding.producer_behavior_id,
    ) == ()
