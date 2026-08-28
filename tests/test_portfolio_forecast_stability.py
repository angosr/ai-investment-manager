from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.estimate import (
    ContextForecastContributionDraft,
    ContextForecastDraft,
    ContextForecastProbabilityDraft,
    ContextForecastStructuredOutput,
    context_forecast_output_schema_for_ids,
)
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityResult,
    ContextForecastStabilityStatus,
    build_context_forecast_stability_assignment,
)
from investment_manager.forecast.contracts import (
    ForecastDecisionSlot,
    ForecastPriceAnchor,
)
from investment_manager.forecast.models import ForecastTarget
from investment_manager.forecast.results import BaseForecast, ForecastBucketProbability
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.market.models import ExecutableQuote
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    PortfolioAccountSnapshot,
    SleeveTarget,
)
from investment_manager.portfolio.stability import (
    CapitalStabilityReplayInputs,
    replay_context_forecast_capital_impact,
)
from investment_manager.settings import load_config

CUTOFF = datetime(2026, 8, 27, 12, tzinfo=UTC)
AVAILABLE = CUTOFF + timedelta(minutes=2)
CONTRACT_ID = "capital-stability-contract"


def test_exact_input_replica_is_replayed_through_the_real_portfolio_engine() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    stability_policy = config.outcome_evaluation.context_forecast_stability
    assert stability_policy is not None
    context_policy = config.capital.context_forecast
    assert context_policy is not None
    target_policy = context_policy.targets[0]
    authorization = CandidateCapitalAuthorization(
        version="capital-stability-counterfactual-v1",
        producer_id=context_policy.producer_id,
        producer_behavior_id=context_policy.producer_behavior_id,
        outcome_family_id=target_policy.outcome_family_id,
        hypothesis_fingerprint="a" * 64,
    )
    instrument = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.symbol == "BTCUSDT"
    )
    slot = ForecastDecisionSlot(
        slot_id=ForecastDecisionSlot.identity_for(CONTRACT_ID, CUTOFF),
        contract_id=CONTRACT_ID,
        slot_as_of=CUTOFF,
        information_cutoff_at=CUTOFF,
        completion_deadline_at=CUTOFF + timedelta(minutes=25),
        evaluation_at=CUTOFF + timedelta(hours=4),
    )
    analysis_input = {
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
                "target_state": {"as_of": CUTOFF.isoformat()},
            }
        ],
        "world_model": {"assessment_id": "world-1"},
    }
    output_schema = context_forecast_output_schema_for_ids(
        decision_slot_ids=(slot.slot_id,),
        bucket_ids=("LOSS", "FLAT", "GAIN"),
        mechanism_ids=("mechanism-1",),
        evidence_ids=("evidence-1",),
    )
    assignment = build_context_forecast_stability_assignment(
        policy=stability_policy,
        slot=slot,
        formal_producer_behavior_id=authorization.producer_behavior_id,
        formal_analysis_input=analysis_input,
        formal_output_schema=output_schema,
        assigned_at=CUTOFF + timedelta(minutes=1),
    )
    target = ForecastTarget.single_long(instrument)
    cutoff_anchor = ForecastPriceAnchor(
        instrument_id=instrument.key,
        price=Decimal("100"),
        observed_at=CUTOFF,
        available_at=CUTOFF,
        quote_ref="cutoff",
    )
    forecast = BaseForecast(
        forecast_id=assignment.targets[0].formal_forecast_id,
        contract_id=CONTRACT_ID,
        decision_slot_id=slot.slot_id,
        producer_id=authorization.producer_id,
        producer_behavior_id=authorization.producer_behavior_id,
        outcome_family_id=authorization.outcome_family_id,
        target=target,
        horizon_minutes=240,
        cutoff_prices=(cutoff_anchor,),
        entry_prices=(
            cutoff_anchor.model_copy(
                update={"available_at": AVAILABLE, "quote_ref": "entry"}
            ),
        ),
        information_cutoff_at=CUTOFF,
        input_observed_at=CUTOFF,
        available_at=AVAILABLE,
        valid_until=AVAILABLE + timedelta(minutes=60),
        outcome_probabilities=(
            ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.1")),
            ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.2")),
            ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.7")),
        ),
        expected_gross_bps=Decimal("60"),
        input_refs=("input-1",),
    )
    account = PortfolioAccountSnapshot(
        snapshot_id="account-1",
        cycle_id="account-cycle-1",
        portfolio_id=config.capital.decision.portfolio_id,
        as_of=AVAILABLE,
        observed_at=AVAILABLE,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
    )
    quote = ExecutableQuote(
        source_quote_id="quote-1",
        instrument=instrument,
        as_of=AVAILABLE,
        observed_at=AVAILABLE,
        bid=Decimal("99.99"),
        bid_quantity=Decimal("1000"),
        ask=Decimal("100.01"),
        ask_quantity=Decimal("1000"),
        source="test",
    )
    sleeve = PortfolioSleeveInput(
        sleeve_id=SleeveTarget.identity_for(
            portfolio_id=config.capital.decision.portfolio_id,
            forecast_family=forecast.outcome_family_id,
            forecast_target_id=forecast.target.target_id,
        ),
        forecast=forecast,
        capital_authorization=authorization,
    )
    decision = PortfolioDecisionEngine(config.capital.decision)
    formal_target = decision.decide(
        cycle_id="formal-cycle",
        as_of=AVAILABLE,
        account=account,
        sleeves=(sleeve,),
        quotes=(quote,),
        execution_specs=config.capital.execution_specs,
    )
    assert formal_target is not None
    assert formal_target.sleeves

    replica_output = ContextForecastStructuredOutput(
        forecasts=(
            ContextForecastDraft(
                decision_slot_id=slot.slot_id,
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
                        mechanism_id="mechanism-1",
                        effect="DOWNSIDE",
                        rationale="同一冻结输入的独立概率估计。",
                    ),
                ),
                evidence_refs=("evidence-1",),
                invalidation_conditions=("价格结构改变",),
            ),
        )
    )
    replica_payload = replica_output.model_dump(mode="json")
    result = ContextForecastStabilityResult(
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
        output_json=canonical_json(replica_payload),
        output_hash=content_hash(replica_payload),
    )

    case = replay_context_forecast_capital_impact(
        assignment=assignment,
        result=result,
        inputs=CapitalStabilityReplayInputs(
            target=formal_target,
            account=account,
            forecasts={forecast.forecast_id: forecast},
            projections={},
        ),
        decision=decision,
        capital_policy=config.capital,
        authorization_by_family={authorization.outcome_family_id: authorization},
    )

    assert not case.formal.cash
    assert case.replica.cash
    assert case.cash_flip
    assert case.expression_flip
    assert case.target_changed
    assert case.maximum_allocation_fraction_delta > 0
