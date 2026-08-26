from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.producer import context_spot_forecast_contract
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
    ForecastMechanismContribution,
    ForecastMechanismEffect,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.forecast.tables import forecasts
from investment_manager.governance.evaluation.outcome_service import (
    configured_world_model_ablation_contract,
    preregister_world_model_ablation_plan,
)
from investment_manager.governance.evaluation.world_model_ablation import (
    SqlWorldModelAblationRepository,
    WorldModelAblationPreallocator,
    WorldModelAblationRunner,
    WorldModelControlStructuredOutput,
    build_world_model_ablation_assignment,
    ensure_world_model_ablation_plan,
)
from investment_manager.governance.models import ReleaseManifest
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.schema import create_schema
from investment_manager.settings import load_config


class _FixedControlAnalyst:
    def __init__(self, completed_at: datetime) -> None:
        self.completed_at = completed_at
        self.calls = 0

    def estimate(self, assignment) -> AnalystResult:
        self.calls += 1
        return AnalystResult(
            success=True,
            output=WorldModelControlStructuredOutput.model_validate(
                {
                    "forecast": {
                        "decision_slot_id": assignment.decision_slot_id,
                        "outcome_probabilities": [
                            {"bucket_id": "LARGE_LOSS", "probability": "0.10"},
                            {"bucket_id": "LOSS", "probability": "0.20"},
                            {"bucket_id": "FLAT", "probability": "0.40"},
                            {"bucket_id": "GAIN", "probability": "0.20"},
                            {"bucket_id": "LARGE_GAIN", "probability": "0.10"},
                        ],
                    }
                }
            ),
            reason_code="CODEX_ANALYSIS_SUCCEEDED",
            account_id=".codex2",
            attempts=1,
            usage={"input_tokens": 100, "output_tokens": 50},
            completed_at=self.completed_at,
            run_id="control-run-1",
        )


def _release(manifest_id: str, *, activation: datetime) -> ReleaseManifest:
    return ReleaseManifest(
        manifest_id=manifest_id,
        created_at=activation - timedelta(hours=2),
        status="CHALLENGER",
        code_version="a" * 40,
        configuration_hash="b" * 64,
        component_versions=(),
        artifacts=(),
        constitution_version="constitution-v1",
    )


def _seed(engine, *, record_formal: bool = True):
    config = load_config("config/investment-manager.shadow.yaml")
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    activation = policy.activated_at
    context = config.capital.context_forecast
    assert context is not None
    instrument = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.key == context.target_instrument_key
    )
    contract = context_spot_forecast_contract(
        policy=context,
        instrument=instrument,
        cost_semantics_version=config.capital.decision.cost_model_version,
    )
    binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id=context.producer_id,
        producer_behavior_id=context.producer_behavior_id,
        permission=ForecastPermission.CAPITAL_CANDIDATE,
        required_feature_keys=context.required_feature_keys,
    )
    cutoff = ForecastPriceAnchor(
        instrument_id=instrument.key,
        price=Decimal("100"),
        observed_at=activation,
        available_at=activation,
        quote_ref="cutoff-quote",
    )
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=activation,
        cutoff_prices=(cutoff,),
    )
    contracts = SqlForecastContractStore(engine)
    contracts.record_contract(contract)
    contracts.record_binding(binding, activated_at=activation)
    contracts.record_slot(slot, binding=binding)
    formal_available = activation + timedelta(minutes=5)
    formal_input = {
        "purpose": "FORECAST_ESTIMATE",
        "decision_slot": {
            "decision_slot_id": slot.slot_id,
            "information_cutoff_at": slot.information_cutoff_at,
            "completion_deadline_at": slot.completion_deadline_at,
            "evaluation_at": slot.evaluation_at,
        },
        "forecast_contract": contract,
        "world_model": {
            "assessment_id": "world-model-1",
            "event_references": [
                {
                    "evidence_id": "evidence-1",
                    "title": "一手事实",
                }
            ],
            "mechanisms": [
                {
                    "mechanism_id": "mechanism-1",
                    "claim": "事实经由流动性传导至风险资产。",
                    "causal_chain": [{"evidence_ids": ["evidence-1"]}],
                    "conflicting_evidence_ids": [],
                }
            ],
        },
        "target_state": {
            "as_of": activation,
            "asset_states": [{"asset": "BTC", "return_fraction": "0.01"}],
            "derivative_states": [],
            "coverage_gap_codes": [],
        },
    }
    probabilities = tuple(
        ForecastBucketProbability(bucket_id=bucket_id, probability=probability)
        for bucket_id, probability in (
            ("LARGE_LOSS", Decimal("0.05")),
            ("LOSS", Decimal("0.10")),
            ("FLAT", Decimal("0.20")),
            ("GAIN", Decimal("0.35")),
            ("LARGE_GAIN", Decimal("0.30")),
        )
    )
    formal = BaseForecast(
        forecast_id=stable_id(
            "base_forecast",
            slot.slot_id,
            context.producer_behavior_id,
        ),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        producer_id=context.producer_id,
        producer_behavior_id=context.producer_behavior_id,
        outcome_family_id=context.outcome_family_id,
        target=contract.target,
        horizon_minutes=contract.horizon_minutes,
        cutoff_prices=(cutoff,),
        entry_prices=(
            ForecastPriceAnchor(
                instrument_id=instrument.key,
                price=Decimal("100.1"),
                observed_at=formal_available,
                available_at=formal_available,
                quote_ref="entry-quote",
            ),
        ),
        information_cutoff_at=activation,
        input_observed_at=activation,
        available_at=formal_available,
        valid_until=formal_available + timedelta(minutes=60),
        outcome_probabilities=probabilities,
        expected_gross_bps=Decimal("87.5"),
        input_refs=("evidence-1", "mechanism-1", "world-model-1"),
        world_model_id="world-model-1",
        mechanism_contributions=(
            ForecastMechanismContribution(
                mechanism_id="mechanism-1",
                effect=ForecastMechanismEffect.UPSIDE,
                rationale="流动性机制提高右尾概率。",
            ),
        ),
        evidence_refs=("evidence-1",),
        invalidation_conditions=("流动性事实被撤销",),
        analysis_input_json=canonical_json(formal_input),
        analysis_input_hash=content_hash(formal_input),
    )
    if record_formal:
        SqlForecastStore(engine).record(formal)
    governance = SqlGovernanceRepository(engine)
    plan = ensure_world_model_ablation_plan(
        governance=governance,
        config=config,
        contract=contract,
        release=_release("release-ablation-v1", activation=activation),
        registered_at=activation - timedelta(hours=1),
    )
    return config, contract, binding, slot, formal, plan


def _preassign(repository, config, plan, slot, formal, *, assigned_at=None):
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    at = assigned_at or slot.information_cutoff_at + timedelta(minutes=1)
    preallocator = WorldModelAblationPreallocator(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=formal.producer_behavior_id,
        repository=repository,
        clock=lambda: at,
    )
    preallocator.before_estimate(
        slot=slot,
        formal_producer_behavior_id=formal.producer_behavior_id,
        formal_analysis_input=json.loads(formal.analysis_input_json),
    )


def _gain_outcome(config, contract, slot) -> ForecastOutcome:
    return ForecastOutcome(
        outcome_id=stable_id(
            "forecast_outcome",
            slot.slot_id,
            config.outcome_evaluation.target_forecast_version,
        ),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        status=ForecastOutcomeStatus.SETTLED,
        information_cutoff_at=slot.information_cutoff_at,
        outcome_start_at=slot.outcome_start_at,
        evaluation_at=slot.evaluation_at,
        settled_at=slot.evaluation_at + timedelta(seconds=1),
        legs=(
            ForecastLegOutcome(
                instrument_id=contract.target.legs[0].instrument.key,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
                reference_price=Decimal("100"),
                exit_price=Decimal("101"),
                price_return_bps=Decimal("100"),
            ),
        ),
        gross_target_return_bps=Decimal("100"),
        realized_bucket_id="GAIN",
        reason_code="SETTLED",
    )


def _record_no_estimate_case(
    engine,
    *,
    config,
    contract,
    binding,
    slot_at: datetime,
    quote_ref: str,
) -> ForecastDecisionSlot:
    cutoff = ForecastPriceAnchor(
        instrument_id=contract.target.legs[0].instrument.key,
        price=Decimal("100"),
        observed_at=slot_at,
        available_at=slot_at,
        quote_ref=quote_ref,
    )
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=slot_at,
        cutoff_prices=(cutoff,),
    )
    contracts = SqlForecastContractStore(engine)
    contracts.record_slot(slot, binding=binding)
    contracts.record_no_estimate(
        ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                slot.slot_id,
                binding.producer_behavior_id,
            ),
            slot_id=slot.slot_id,
            contract_id=contract.contract_id,
            producer_kind=ForecastProducerKind.CONTEXT,
            producer_id=binding.producer_id,
            producer_behavior_id=binding.producer_behavior_id,
            reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot_at,
            completed_at=slot_at,
        )
    )
    SqlForecastStore(engine).record_outcome(_gain_outcome(config, contract, slot))
    return slot


def test_preregistration_and_runtime_share_one_formal_contract() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    release = _release("release-preregister-v1", activation=policy.activated_at)

    plan = preregister_world_model_ablation_plan(
        config=config,
        engine=engine,
        release=release,
        registered_at=policy.activated_at - timedelta(hours=1),
    )
    contract = configured_world_model_ablation_contract(config)

    assert plan.candidate_spec_snapshot["formal_contract_id"] == contract.contract_id
    assert plan.candidate_spec_snapshot["sample_selection"] == (
        "GREEDY_NON_OVERLAPPING_INFORMATION_CUTOFF_TO_EVALUATION_V1"
    )
    assert plan.candidate_spec_snapshot["uncertainty_method"] == (
        "NEWEY_WEST_LAG_1_ON_NON_OVERLAPPING_V1"
    )
    assert "OVERLAPPING_OUTCOME_WINDOWS_COUNT_ONCE" in plan.hard_guardrails
    assert (
        preregister_world_model_ablation_plan(
            config=config,
            engine=engine,
            release=release,
            registered_at=policy.activated_at - timedelta(minutes=30),
        )
        == plan
    )


def test_control_assignment_removes_world_model_and_freezes_shared_contract() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    _config, _contract, _binding, slot, formal, plan = _seed(
        engine,
        record_formal=False,
    )

    assignment = build_world_model_ablation_assignment(
        plan=plan,
        slot=slot,
        formal_producer_behavior_id=formal.producer_behavior_id,
        formal_analysis_input=json.loads(formal.analysis_input_json),
        assigned_at=slot.information_cutoff_at + timedelta(minutes=1),
    )
    control_input = json.loads(assignment.control_input_json)
    formal_input = json.loads(formal.analysis_input_json)

    assert "world_model" not in control_input
    assert control_input["forecast_contract"] == formal_input["forecast_contract"]
    assert control_input["target_state"] == formal_input["target_state"]
    assert assignment.formal_analysis_input_hash == formal.analysis_input_hash
    assert assignment.formal_available_at is None
    assert assignment.call_order == "PREASSIGNED_INDEPENDENT_WORKERS"
    output_schema = json.loads(assignment.output_schema_json)
    control_fields = output_schema["$defs"]["WorldModelControlForecastDraft"]["properties"]
    assert set(control_fields) == {"decision_slot_id", "outcome_probabilities"}
    repository = SqlWorldModelAblationRepository(engine)
    assert repository.record_assignment(assignment)
    assert repository.assignment(assignment.assignment_id) == assignment


def test_preassigned_control_runs_without_waiting_for_formal_forecast() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, _contract, _binding, slot, formal, plan = _seed(
        engine,
        record_formal=False,
    )
    repository = SqlWorldModelAblationRepository(engine)
    _preassign(repository, config, plan, slot, formal)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    analyst = _FixedControlAnalyst(slot.information_cutoff_at + timedelta(minutes=3))
    runner = WorldModelAblationRunner(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=formal.producer_behavior_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        repository=repository,
        analyst=analyst,
    )

    report = runner.reconcile(as_of=slot.information_cutoff_at + timedelta(minutes=2))

    assert analyst.calls == 1
    assert report.assignments == 1
    assert report.successful_controls == 1
    assert report.formal_forecast_count == 0
    assert report.settled_pairs == 0
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(forecasts)) == 0


def test_preassignment_retry_rejects_input_drift() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, _contract, _binding, slot, formal, plan = _seed(
        engine,
        record_formal=False,
    )
    repository = SqlWorldModelAblationRepository(engine)
    _preassign(repository, config, plan, slot, formal)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    preallocator = WorldModelAblationPreallocator(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=formal.producer_behavior_id,
        repository=repository,
        clock=lambda: slot.information_cutoff_at + timedelta(minutes=2),
    )
    changed_input = json.loads(formal.analysis_input_json)
    changed_input["target_state"]["asset_states"][0]["return_fraction"] = "0.02"

    with pytest.raises(ValueError, match="重试绑定了不同输入"):
        preallocator.before_estimate(
            slot=slot,
            formal_producer_behavior_id=formal.producer_behavior_id,
            formal_analysis_input=changed_input,
        )


def test_plan_is_prospective_and_survives_unrelated_release_changes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, contract, _binding, _slot, _formal, first = _seed(engine)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None

    repeated = ensure_world_model_ablation_plan(
        governance=SqlGovernanceRepository(engine),
        config=config,
        contract=contract,
        release=_release("release-ablation-v2", activation=policy.activated_at),
        registered_at=policy.activated_at - timedelta(minutes=30),
    )

    assert repeated == first
    assert repeated.base_manifest_id == "release-ablation-v1"


def test_runner_is_idempotent_and_scores_same_slot_without_capital_output() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, contract, _binding, slot, formal, plan = _seed(engine)
    analyst = _FixedControlAnalyst(formal.available_at + timedelta(minutes=2))
    repository = SqlWorldModelAblationRepository(engine)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    runner = WorldModelAblationRunner(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=formal.producer_behavior_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        repository=repository,
        analyst=analyst,
    )
    _preassign(repository, config, plan, slot, formal)

    first = runner.reconcile(as_of=formal.available_at + timedelta(minutes=1))
    replay = runner.reconcile(as_of=formal.available_at + timedelta(minutes=2))

    assert first.assignments == replay.assignments == 1
    assert analyst.calls == 1
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(forecasts)).scalar_one() == 1

    outcome = _gain_outcome(config, contract, slot)
    SqlForecastStore(engine).record_outcome(outcome)
    report = repository.report(
        plan_id=plan.plan_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        minimum_sample_size=policy.minimum_sample_size,
        formal_producer_behavior_id=formal.producer_behavior_id,
        activated_at=policy.activated_at,
        as_of=outcome.settled_at,
    )

    assert report.settled_pairs == 1
    assert report.formal_forecast_count == 1
    assert report.formal_no_estimate_count == 0
    assert report.successful_controls == 1
    assert report.conservative_sample_count == 1
    assert report.mean_brier_improvement is not None
    assert report.mean_brier_improvement > 0
    assert report.conservative_mean_brier_improvement == report.mean_brier_improvement
    assert not report.evidence_sufficient


def test_late_control_is_counted_as_failure_without_post_outcome_retry() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, contract, _binding, slot, formal, plan = _seed(engine)
    analyst = _FixedControlAnalyst(slot.completion_deadline_at + timedelta(minutes=1))
    repository = SqlWorldModelAblationRepository(engine)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    runner = WorldModelAblationRunner(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=formal.producer_behavior_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        repository=repository,
        analyst=analyst,
    )
    _preassign(repository, config, plan, slot, formal)

    first = runner.reconcile(as_of=slot.completion_deadline_at + timedelta(seconds=1))
    replay = runner.reconcile(as_of=slot.completion_deadline_at + timedelta(minutes=1))

    assert first.failed_controls == replay.failed_controls == 1
    assert first.successful_controls == replay.successful_controls == 0
    assert analyst.calls == 0

    outcome = _gain_outcome(config, contract, slot)
    SqlForecastStore(engine).record_outcome(outcome)
    report = repository.report(
        plan_id=plan.plan_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        minimum_sample_size=policy.minimum_sample_size,
        formal_producer_behavior_id=formal.producer_behavior_id,
        activated_at=policy.activated_at,
        as_of=outcome.settled_at,
    )

    assert report.settled_pairs == 0
    assert report.conservative_sample_count == 1
    assert report.conservative_mean_brier_improvement is not None
    assert report.conservative_mean_brier_improvement < 0
    assert not report.evidence_sufficient


def test_formal_no_estimate_enters_conservative_skill_bound() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, contract, binding, _slot, formal, plan = _seed(engine)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    missing_at = policy.activated_at + timedelta(hours=4)
    cutoff = ForecastPriceAnchor(
        instrument_id=contract.target.legs[0].instrument.key,
        price=Decimal("100"),
        observed_at=missing_at,
        available_at=missing_at,
        quote_ref="missing-cutoff-quote",
    )
    missing_slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=missing_at,
        cutoff_prices=(cutoff,),
    )
    contracts = SqlForecastContractStore(engine)
    contracts.record_slot(missing_slot, binding=binding)
    contracts.record_no_estimate(
        ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                missing_slot.slot_id,
                formal.producer_behavior_id,
            ),
            slot_id=missing_slot.slot_id,
            contract_id=contract.contract_id,
            producer_kind=ForecastProducerKind.CONTEXT,
            producer_id=formal.producer_id,
            producer_behavior_id=formal.producer_behavior_id,
            reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
            information_cutoff_at=missing_slot.information_cutoff_at,
            attempted_at=missing_at,
            completed_at=missing_at,
        )
    )
    outcome = _gain_outcome(config, contract, missing_slot)
    SqlForecastStore(engine).record_outcome(outcome)
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None

    report = SqlWorldModelAblationRepository(engine).report(
        plan_id=plan.plan_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        minimum_sample_size=policy.minimum_sample_size,
        formal_producer_behavior_id=formal.producer_behavior_id,
        activated_at=policy.activated_at,
        as_of=outcome.settled_at,
    )

    assert report.formal_no_estimate_count == 1
    assert report.settled_pairs == 0
    assert report.conservative_sample_count == 1
    assert report.conservative_mean_brier_improvement == Decimal("-2")
    assert not report.evidence_sufficient


def test_report_counts_only_deterministic_non_overlapping_skill_samples() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config, contract, binding, _slot, _formal, plan = _seed(
        engine,
        record_formal=False,
    )
    policy = config.outcome_evaluation.world_model_ablation
    assert policy is not None
    for hours in (1, 4, 8):
        _record_no_estimate_case(
            engine,
            config=config,
            contract=contract,
            binding=binding,
            slot_at=policy.activated_at + timedelta(hours=hours),
            quote_ref=f"missing-{hours}",
        )

    report = SqlWorldModelAblationRepository(engine).report(
        plan_id=plan.plan_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        minimum_sample_size=2,
        formal_producer_behavior_id=binding.producer_behavior_id,
        activated_at=policy.activated_at,
        as_of=policy.activated_at + timedelta(hours=9),
    )

    assert report.formal_no_estimate_count == 3
    assert report.conservative_sample_count == 2
    assert report.conservative_mean_brier_improvement == Decimal("-2")
    assert not report.evidence_sufficient
