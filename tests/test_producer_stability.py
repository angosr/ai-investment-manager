from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.estimate import (
    ContextForecastContributionDraft,
    ContextForecastDraft,
    ContextForecastProbabilityDraft,
    ContextForecastStructuredOutput,
    QuantContextPosteriorDraft,
    QuantContextPosteriorStructuredOutput,
    context_forecast_output_schema_for_ids,
)
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityAssignment,
    ContextForecastStabilityResult,
    ContextForecastStabilityStatus,
    build_context_forecast_stability_assignment,
)
from investment_manager.forecast.contracts import (
    ForecastBenchmarkProbability,
    ForecastContract,
    ForecastDecisionSlot,
    ForecastOutcomeBucket,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotObligation,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.forecast.product.models import (
    ProductProjectionState,
    project_product_payoff,
)
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastMechanismContribution,
    ForecastMechanismEffect,
)
from investment_manager.governance.evaluation.logical_account import (
    ProducerDecisionPanel,
    ProducerPanelLedger,
)
from investment_manager.governance.evaluation.producer_stability import (
    PortfolioForecastStabilityEvaluator,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.perpetual.models import PerpetualQuote
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.settings import load_config

CUTOFF = datetime(2026, 8, 27, 12, tzinfo=UTC)
BEHAVIOR_ID = "a" * 64
MECHANISM_ID = "mechanism-1"
EVIDENCE_ID = "evidence-1"


def _target(
    instrument: InstrumentId,
    direction: ExposureDirection = ExposureDirection.LONG,
) -> ForecastTarget:
    return ForecastTarget.create(
        (
            ForecastLeg(
                instrument=instrument,
                direction=direction,
                gross_weight=Decimal("1"),
            ),
        )
    )


def _contract(instrument: InstrumentId) -> ForecastContract:
    buckets = (
        ForecastOutcomeBucket(
            bucket_id="LOSS",
            upper_bps=Decimal("-50"),
            representative_bps=Decimal("-100"),
        ),
        ForecastOutcomeBucket(
            bucket_id="FLAT",
            lower_bps=Decimal("-50"),
            upper_bps=Decimal("50"),
            representative_bps=Decimal("0"),
        ),
        ForecastOutcomeBucket(
            bucket_id="GAIN",
            lower_bps=Decimal("50"),
            representative_bps=Decimal("100"),
        ),
    )
    return ForecastContract.create(
        contract_version="stability-test-v1",
        outcome_family_id="btc-reference-4h",
        target=_target(instrument),
        outcome_buckets=buckets,
        horizon_minutes=240,
        decision_slot_rule="test",
        evaluation_trigger="test",
        information_cutoff_rule="test",
        completion_deadline_seconds=300,
        minimum_remaining_horizon_minutes=120,
        entry_anchor_rule="test",
        cost_semantics_version="test",
        validity_minutes=60,
        validity_conditions=("test",),
        settlement_rule="test",
        forecast_benchmark=tuple(
            ForecastBenchmarkProbability(
                bucket_id=item.bucket_id,
                probability=probability,
            )
            for item, probability in zip(
                buckets,
                (Decimal("0.25"), Decimal("0.5"), Decimal("0.25")),
                strict=True,
            )
        ),
        decision_benchmark="test",
    )


def _analysis_input(slot: ForecastDecisionSlot, contract: ForecastContract) -> dict:
    return {
        "purpose": "FORECAST_ESTIMATE",
        "forecast_targets": [
            {
                "decision_slot": {
                    "decision_slot_id": slot.slot_id,
                    "information_cutoff_at": CUTOFF.isoformat().replace("+00:00", "Z"),
                    "completion_deadline_at": slot.completion_deadline_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "evaluation_at": slot.evaluation_at.isoformat().replace("+00:00", "Z"),
                },
                "forecast_contract": {
                    "contract_id": contract.contract_id,
                    "outcome_buckets": [
                        {
                            "bucket_id": item.bucket_id,
                            "representative_bps": str(item.representative_bps),
                        }
                        for item in contract.outcome_buckets
                    ],
                },
                "target_state": {"as_of": CUTOFF.isoformat()},
            }
        ],
        "world_model": {
            "assessment_id": "world-1",
            "mechanisms": [{"mechanism_id": MECHANISM_ID}],
            "evidence": [{"evidence_id": EVIDENCE_ID}],
        },
    }


def _forecast(
    *,
    contract: ForecastContract,
    slot: ForecastDecisionSlot,
    analysis_input: dict,
) -> BaseForecast:
    available_at = CUTOFF + timedelta(minutes=1)
    anchor = ForecastPriceAnchor(
        instrument_id=contract.target.legs[0].instrument.key,
        price=Decimal("100.1"),
        observed_at=available_at,
        available_at=available_at,
        quote_ref="formal-entry",
    )
    return BaseForecast(
        forecast_id=stable_id("base_forecast", slot.slot_id, BEHAVIOR_ID),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        producer_id="context-test",
        producer_behavior_id=BEHAVIOR_ID,
        outcome_family_id=contract.outcome_family_id,
        target=contract.target,
        horizon_minutes=contract.horizon_minutes,
        cutoff_prices=(
            anchor.model_copy(
                update={
                    "observed_at": CUTOFF,
                    "available_at": CUTOFF,
                    "quote_ref": "cutoff",
                }
            ),
        ),
        entry_prices=(anchor,),
        information_cutoff_at=CUTOFF,
        input_observed_at=CUTOFF,
        available_at=available_at,
        valid_until=available_at + timedelta(minutes=60),
        outcome_probabilities=(
            ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.1")),
            ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.2")),
            ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.7")),
        ),
        expected_gross_bps=Decimal("60"),
        input_refs=("input-1",),
        world_model_id="world-1",
        mechanism_contributions=(
            ForecastMechanismContribution(
                mechanism_id=MECHANISM_ID,
                effect=ForecastMechanismEffect.UPSIDE,
                rationale="冻结输入支持上行情景。",
            ),
        ),
        evidence_refs=(EVIDENCE_ID,),
        invalidation_conditions=("市场状态反转",),
        analysis_input_json=canonical_json(analysis_input),
        analysis_input_hash=content_hash(analysis_input),
    )


def _fixture():
    config = load_config("config/investment-manager.shadow.yaml")
    stability_policy = config.outcome_evaluation.context_forecast_stability
    assert stability_policy is not None
    instrument = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.symbol == "BTCUSDT"
        and item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    contract = _contract(instrument)
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=CUTOFF,
        cutoff_prices=(),
    )
    analysis_input = _analysis_input(slot, contract)
    assignment = build_context_forecast_stability_assignment(
        policy=stability_policy,
        slot=slot,
        formal_producer_behavior_id=BEHAVIOR_ID,
        formal_analysis_input=analysis_input,
        formal_output_schema=context_forecast_output_schema_for_ids(
            decision_slot_ids=(slot.slot_id,),
            bucket_ids=("LOSS", "FLAT", "GAIN"),
            mechanism_ids=(MECHANISM_ID,),
            evidence_ids=(EVIDENCE_ID,),
        ),
        assigned_at=CUTOFF + timedelta(seconds=30),
    )
    forecast = _forecast(contract=contract, slot=slot, analysis_input=analysis_input)
    binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id=forecast.producer_id,
        producer_behavior_id=BEHAVIOR_ID,
        permission=ForecastPermission.RESEARCH,
    )
    panel = ProducerDecisionPanel(
        panel_id="formal-panel",
        producer_id=forecast.producer_id,
        producer_behavior_id=BEHAVIOR_ID,
        slot_as_of=CUTOFF,
        information_cutoff_at=CUTOFF,
        available_at=forecast.available_at,
        obligations=(ForecastSlotObligation.create(slot=slot, binding=binding),),
        slots=(slot,),
        forecasts=(forecast,),
        no_estimates=(),
    )
    ledger = ProducerPanelLedger(
        producer_behavior_id=BEHAVIOR_ID,
        as_of=CUTOFF + timedelta(minutes=3),
        obligated_panel_count=1,
        complete_panels=(panel,),
        pending_panel_count=0,
    )
    market = InMemoryMarketDataStore()
    for index, observed_at in enumerate(
        (forecast.available_at, CUTOFF + timedelta(minutes=2)),
        start=1,
    ):
        market.put_perpetual_quote(
            PerpetualQuote(
                quote_id=stable_id("perpetual_quote", instrument.key, index),
                instrument=instrument,
                exchange_time=observed_at,
                observed_at=observed_at,
                bid=Decimal("99.9"),
                bid_quantity=Decimal("100"),
                ask=Decimal("100.1"),
                ask_quantity=Decimal("100"),
                update_id=index,
                source="test",
            )
        )

    class Builder:
        @staticmethod
        def build(source: BaseForecast, *, as_of: datetime):
            return tuple(
                project_product_payoff(
                    contract=contract,
                    forecast=source,
                    state=ProductProjectionState(
                        target=_target(instrument, direction),
                        entry_anchor=ForecastPriceAnchor(
                            instrument_id=instrument.key,
                            price=Decimal("100.1"),
                            observed_at=as_of,
                            available_at=as_of,
                            quote_ref=f"projection-{direction.value}-{as_of.isoformat()}",
                        ),
                        valid_until=min(
                            source.valid_until,
                            as_of + timedelta(minutes=5),
                        ),
                        expected_exit_basis_bps=Decimal("0"),
                        expected_funding_bps=Decimal("0"),
                        mapping_uncertainty_bps=Decimal("1"),
                        initial_margin_fraction=Decimal("0.1"),
                        product_rule_refs=("test-rule",),
                        input_refs=("test-state",),
                    ),
                    economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
                    projection_version="stability-test-v1",
                )
                for direction in (ExposureDirection.LONG, ExposureDirection.SHORT)
            )

    evaluator = PortfolioForecastStabilityEvaluator(
        capital_policy=config.capital.model_copy(update={"enabled": True}),
        initial_cash=Decimal("10000"),
        market=market,
        product_payoffs_by_family={contract.outcome_family_id: Builder()},
        sleeve_risk=config.capital.sleeve_risk,
    )
    return assignment, ledger, evaluator


def _result(
    assignment: ContextForecastStabilityAssignment,
    *,
    status: ContextForecastStabilityStatus,
) -> ContextForecastStabilityResult:
    output = None
    if status == ContextForecastStabilityStatus.SUCCEEDED:
        output = ContextForecastStructuredOutput(
            forecasts=(
                ContextForecastDraft(
                    decision_slot_id=assignment.targets[0].decision_slot_id,
                    outcome_probabilities=(
                        ContextForecastProbabilityDraft(
                            bucket_id="LOSS", probability="0.7"
                        ),
                        ContextForecastProbabilityDraft(
                            bucket_id="FLAT", probability="0.2"
                        ),
                        ContextForecastProbabilityDraft(
                            bucket_id="GAIN", probability="0.1"
                        ),
                    ),
                    mechanism_contributions=(
                        ContextForecastContributionDraft(
                            mechanism_id=MECHANISM_ID,
                            effect="DOWNSIDE",
                            rationale="同一冻结输入的独立概率估计。",
                        ),
                    ),
                    evidence_refs=(EVIDENCE_ID,),
                    invalidation_conditions=("价格结构改变",),
                ),
            )
        ).model_dump(mode="json")
    return ContextForecastStabilityResult(
        result_id=stable_id(
            "context_forecast_stability_result",
            assignment.assignment_id,
            1,
        ),
        assignment_id=assignment.assignment_id,
        replica_index=1,
        status=status,
        completed_at=CUTOFF + timedelta(minutes=2),
        reason_code=(
            "STABILITY_REPLICA_SUCCEEDED"
            if output is not None
            else "STABILITY_REPLICA_FAILED"
        ),
        output_json=None if output is None else canonical_json(output),
        output_hash=None if output is None else content_hash(output),
    )


def test_research_replica_replays_independent_cost_after_capital_paths() -> None:
    assignment, ledger, evaluator = _fixture()

    report = evaluator.evaluate(
        formal_ledger=ledger,
        assignments=(assignment,),
        results=(
            _result(
                assignment,
                status=ContextForecastStabilityStatus.SUCCEEDED,
            ),
        ),
    )

    assert report.replayable_case_count == 1
    assert report.unreplayable_case_count == 0
    assert report.expression_flip_count == 1
    assert report.target_change_count == 1
    assert report.maximum_allocation_fraction_delta is not None
    assert report.maximum_allocation_fraction_delta > 0
    assert report.maximum_absolute_turnover_delta is not None


def test_posterior_noop_with_empty_evidence_replays_capital_path() -> None:
    assignment, ledger, evaluator = _fixture()
    formal = ledger.complete_panels[0].forecasts[0]
    analysis_input = _analysis_input(
        ledger.complete_panels[0].slots[0], _contract(formal.target.legs[0].instrument)
    )
    analysis_input["purpose"] = "QUANT_CONTEXT_POSTERIOR"
    analysis_input["forecast_targets"][0]["quant_panel"] = {
        "quant_prior": {
            "outcome_probabilities": [
                {
                    "bucket_id": item.bucket_id,
                    "probability": str(item.probability),
                }
                for item in formal.outcome_probabilities
            ]
        }
    }
    analysis_input["world_model"]["mechanisms"] = [
        {
            "mechanism_id": MECHANISM_ID,
            "evidence_ids": [EVIDENCE_ID],
            "conflicting_evidence_ids": [],
            "structural_evidence_ids": [EVIDENCE_ID],
        }
    ]
    assignment = assignment.model_copy(
        update={
            "formal_analysis_input_json": canonical_json(analysis_input),
            "formal_analysis_input_hash": content_hash(analysis_input),
        }
    )
    output = QuantContextPosteriorStructuredOutput(
        forecasts=(
            QuantContextPosteriorDraft(
                decision_slot_id=formal.decision_slot_id,
                outcome_probabilities=tuple(
                    ContextForecastProbabilityDraft(
                        bucket_id=item.bucket_id,
                        probability=str(item.probability),
                    )
                    for item in formal.outcome_probabilities
                ),
                mechanism_contributions=(
                    ContextForecastContributionDraft(
                        mechanism_id=MECHANISM_ID,
                        effect="NO_MATERIAL_EFFECT",
                        rationale="结构事实不足以改变 Quant prior。",
                    ),
                ),
                evidence_refs=(),
                invalidation_conditions=("结构传导得到确认",),
            ),
        )
    )
    result = ContextForecastStabilityResult(
        result_id=stable_id(
            "context_forecast_stability_result",
            assignment.assignment_id,
            1,
        ),
        assignment_id=assignment.assignment_id,
        replica_index=1,
        status=ContextForecastStabilityStatus.SUCCEEDED,
        completed_at=CUTOFF + timedelta(minutes=2),
        reason_code="STABILITY_REPLICA_SUCCEEDED",
        output_json=canonical_json(output.model_dump(mode="json")),
        output_hash=content_hash(output.model_dump(mode="json")),
    )

    report = evaluator.evaluate(
        formal_ledger=ledger,
        assignments=(assignment,),
        results=(result,),
    )

    assert report.replayable_case_count == 1
    assert report.unreplayable_case_count == 0


def test_failed_replica_is_a_no_estimate_capital_path_instead_of_disappearing() -> None:
    assignment, ledger, evaluator = _fixture()

    report = evaluator.evaluate(
        formal_ledger=ledger,
        assignments=(assignment,),
        results=(
            _result(
                assignment,
                status=ContextForecastStabilityStatus.FAILED,
            ),
        ),
    )

    assert report.successful_replica_count == 0
    assert report.replayable_case_count == 1
    assert report.unreplayable_case_count == 0
    assert report.cash_flip_count == 1
    assert report.expression_flip_count == 1
    assert report.maximum_absolute_turnover_delta is not None
