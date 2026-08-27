from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.estimate import (
    ContextForecastContributionDraft,
    ContextForecastDraft,
    ContextForecastProbabilityDraft,
    ContextForecastStructuredOutput,
    context_forecast_output_schema_for_ids,
)
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityResult,
    ContextForecastStabilityRunner,
    ContextForecastStabilityStatus,
    SqlContextForecastStabilityRepository,
    build_context_forecast_stability_assignment,
    evaluate_context_forecast_stability,
)
from investment_manager.forecast.contracts import (
    ForecastDecisionSlot,
    ForecastPriceAnchor,
)
from investment_manager.forecast.models import ForecastTarget
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

CUTOFF = datetime(2026, 8, 27, 12, tzinfo=UTC)
BEHAVIOR = "a" * 64
CONTRACT_ID = "contract-stability"


def _slot() -> ForecastDecisionSlot:
    return ForecastDecisionSlot(
        slot_id=ForecastDecisionSlot.identity_for(CONTRACT_ID, CUTOFF),
        contract_id=CONTRACT_ID,
        slot_as_of=CUTOFF,
        information_cutoff_at=CUTOFF,
        completion_deadline_at=CUTOFF + timedelta(minutes=25),
        evaluation_at=CUTOFF + timedelta(hours=4),
    )


def _input(slot: ForecastDecisionSlot) -> dict[str, object]:
    return {
        "purpose": "FORECAST_ESTIMATE",
        "forecast_targets": [
            {
                "decision_slot": {
                    "decision_slot_id": slot.slot_id,
                    "information_cutoff_at": "2026-08-27T12:00:00Z",
                    "completion_deadline_at": "2026-08-27T12:25:00Z",
                    "evaluation_at": "2026-08-27T16:00:00Z",
                },
                "forecast_contract": {
                    "contract_id": CONTRACT_ID,
                    "outcome_buckets": [
                        {"bucket_id": "LOSS", "representative_bps": "-100"},
                        {"bucket_id": "FLAT", "representative_bps": "0"},
                        {"bucket_id": "GAIN", "representative_bps": "100"},
                    ],
                },
                "target_state": {"as_of": "2026-08-27T12:00:00Z"},
            }
        ],
        "world_model": {"assessment_id": "world-1"},
    }


def _schema(slot: ForecastDecisionSlot) -> dict[str, object]:
    return context_forecast_output_schema_for_ids(
        decision_slot_ids=(slot.slot_id,),
        bucket_ids=("LOSS", "FLAT", "GAIN"),
        mechanism_ids=("mechanism-1",),
        evidence_ids=("evidence-1",),
    )


def _assignment():
    config = load_config("config/investment-manager.shadow.yaml")
    policy = config.outcome_evaluation.context_forecast_stability
    assert policy is not None
    slot = _slot()
    return policy, build_context_forecast_stability_assignment(
        policy=policy,
        slot=slot,
        formal_producer_behavior_id=BEHAVIOR,
        formal_analysis_input=_input(slot),
        formal_output_schema=_schema(slot),
        assigned_at=CUTOFF + timedelta(minutes=1),
    )


def _output(probabilities: tuple[str, str, str]) -> ContextForecastStructuredOutput:
    return ContextForecastStructuredOutput(
        forecasts=(
            ContextForecastDraft(
                decision_slot_id=_slot().slot_id,
                outcome_probabilities=tuple(
                    ContextForecastProbabilityDraft(
                        bucket_id=bucket_id,
                        probability=probability,
                    )
                    for bucket_id, probability in zip(
                        ("LOSS", "FLAT", "GAIN"),
                        probabilities,
                        strict=True,
                    )
                ),
                mechanism_contributions=(
                    ContextForecastContributionDraft(
                        mechanism_id="mechanism-1",
                        effect="UPSIDE",
                        rationale="可验证的测试机制。",
                    ),
                ),
                evidence_refs=("evidence-1",),
                invalidation_conditions=("可观察条件",),
            ),
        )
    )


def _formal(assignment) -> BaseForecast:
    config = load_config("config/investment-manager.yaml")
    instrument = next(
        item
        for item in config.capital.forecast_reference_instruments
        if item.symbol == "BTCUSDT"
    )
    target = ForecastTarget.single_long(instrument)
    cutoff = ForecastPriceAnchor(
        instrument_id=instrument.key,
        price=Decimal("100"),
        observed_at=CUTOFF,
        available_at=CUTOFF,
        quote_ref="cutoff",
    )
    available = CUTOFF + timedelta(minutes=2)
    entry = ForecastPriceAnchor(
        instrument_id=instrument.key,
        price=Decimal("100"),
        observed_at=available,
        available_at=available,
        quote_ref="entry",
    )
    target_id = assignment.targets[0].decision_slot_id
    return BaseForecast(
        forecast_id=stable_id("base_forecast", target_id, BEHAVIOR),
        contract_id=CONTRACT_ID,
        decision_slot_id=target_id,
        producer_id="context-forecast",
        producer_behavior_id=BEHAVIOR,
        outcome_family_id="btc-4h",
        target=target,
        horizon_minutes=240,
        cutoff_prices=(cutoff,),
        entry_prices=(entry,),
        information_cutoff_at=CUTOFF,
        input_observed_at=CUTOFF,
        available_at=available,
        valid_until=available + timedelta(minutes=60),
        outcome_probabilities=(
            ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.20")),
            ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.50")),
            ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.30")),
        ),
        expected_gross_bps=Decimal("10"),
        input_refs=("input-1",),
    )


def _result(assignment, output) -> ContextForecastStabilityResult:
    payload = output.model_dump(mode="json")
    return ContextForecastStabilityResult(
        result_id=stable_id(
            "context_forecast_stability_result",
            assignment.assignment_id,
            1,
        ),
        assignment_id=assignment.assignment_id,
        replica_index=1,
        status=ContextForecastStabilityStatus.SUCCEEDED,
        completed_at=CUTOFF + timedelta(minutes=3),
        reason_code="STABILITY_REPLICA_SUCCEEDED",
        output_json=canonical_json(payload),
        output_hash=content_hash(payload),
    )


def test_assignment_freezes_the_exact_formal_prompt_and_schema() -> None:
    _policy, assignment = _assignment()

    assert assignment.formal_analysis_input_json == canonical_json(_input(_slot()))
    assert assignment.formal_prompt.endswith(assignment.formal_analysis_input_json)
    assert assignment.formal_output_schema_json == canonical_json(_schema(_slot()))
    assert assignment.replicas_per_input == 1


def test_assignment_identity_uses_frozen_target_order_not_caller_anchor() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    policy = config.outcome_evaluation.context_forecast_stability
    assert policy is not None
    first = _slot()
    second = first.model_copy(
        update={
            "slot_id": stable_id("forecast_decision_slot", "second"),
            "contract_id": "contract-second",
        }
    )
    analysis_input = _input(first)
    second_target = dict(analysis_input["forecast_targets"][0])
    second_target["decision_slot"] = dict(second_target["decision_slot"])
    second_target["decision_slot"]["decision_slot_id"] = second.slot_id
    second_target["forecast_contract"] = dict(second_target["forecast_contract"])
    second_target["forecast_contract"]["contract_id"] = second.contract_id
    analysis_input["forecast_targets"].append(second_target)
    schema = context_forecast_output_schema_for_ids(
        decision_slot_ids=(first.slot_id, second.slot_id),
        bucket_ids=("LOSS", "FLAT", "GAIN"),
        mechanism_ids=("mechanism-1",),
        evidence_ids=("evidence-1",),
    )

    assignment = build_context_forecast_stability_assignment(
        policy=policy,
        slot=second,
        formal_producer_behavior_id=BEHAVIOR,
        formal_analysis_input=analysis_input,
        formal_output_schema=schema,
        assigned_at=CUTOFF + timedelta(minutes=1),
    )

    assert assignment.assignment_id == stable_id(
        "context_forecast_stability_assignment",
        policy.version,
        BEHAVIOR,
        stable_id("base_forecast", first.slot_id, BEHAVIOR),
    )


def test_stability_evaluation_measures_distribution_and_direction_drift() -> None:
    policy, assignment = _assignment()
    formal = _formal(assignment)
    replica = _output(("0.35", "0.50", "0.15"))

    report = evaluate_context_forecast_stability(
        policy=policy,
        formal_producer_behavior_id=BEHAVIOR,
        assignments=(assignment,),
        results=(_result(assignment, replica),),
        formal_forecasts={formal.forecast_id: formal},
        as_of=CUTOFF + timedelta(minutes=4),
    )

    assert report.complete_sample_count == 1
    assert report.mean_max_total_variation == Decimal("0.15")
    assert report.maximum_expected_gross_difference_bps == Decimal("30")
    assert report.canonical_direction_flip_count == 1


def test_runner_records_one_replica_and_replay_does_not_call_codex_again() -> None:
    class Analyst:
        def __init__(self) -> None:
            self.calls = 0

        def estimate(self, assignment, replica_index):
            self.calls += 1
            return AnalystResult(
                True,
                _output(("0.20", "0.50", "0.30")),
                "CODEX_ANALYSIS_SUCCEEDED",
                account_id=".codex",
                attempts=1,
                completed_at=CUTOFF + timedelta(minutes=3),
                run_id="run-1",
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    policy, assignment = _assignment()
    repository = SqlContextForecastStabilityRepository(engine)
    repository.record_assignment(assignment)
    analyst = Analyst()
    runner = ContextForecastStabilityRunner(
        policy=policy,
        formal_producer_behavior_id=BEHAVIOR,
        repository=repository,
        analyst=analyst,
    )

    first = runner.reconcile(as_of=CUTOFF + timedelta(minutes=2))
    second = runner.reconcile(as_of=CUTOFF + timedelta(minutes=4))

    assert first.successful_replica_count == second.successful_replica_count == 1
    assert analyst.calls == 1
    assert len(repository.results((assignment.assignment_id,))) == 1
