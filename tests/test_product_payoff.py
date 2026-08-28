from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from investment_manager.decision_cycle.capital import CapitalCycleService
from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlanner,
    TradePlannerPolicy,
)
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastBenchmarkProbability,
    ForecastContract,
    ForecastDecisionSlot,
    ForecastOutcomeBucket,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
    ForecastSlotObligation,
    ForecastSlotStratum,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.forecast.product.context import ContextProductPayoffProjector
from investment_manager.forecast.product.evaluation import (
    ProductPayoffEvaluationCase,
    ProductPayoffEvidenceStatus,
    ProductPayoffMappingIdentity,
    evaluate_product_payoff_evidence,
)
from investment_manager.forecast.product.models import (
    ProductPayoffOutcome,
    ProductProjectionState,
    project_product_payoff,
)
from investment_manager.forecast.product.repository import SqlProductPayoffProjectionStore
from investment_manager.forecast.product.settlement import ProductPayoffOutcomeSettler
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.governance.evaluation.logical_account import (
    ProducerDecisionPanel,
    ProducerLogicalAccount,
    ProducerPanelLedger,
    SqlProducerPanelReader,
)
from investment_manager.governance.evaluation.producer_capital import (
    ProducerCapitalReplay,
    ProducerProductProjectionRecorder,
    evaluate_producer_capital_path,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
)
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualProductRules,
    PerpetualQuote,
    perpetual_product_rule_content_hash,
)
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioDecisionPolicy,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    InstrumentPosition,
    PortfolioAccountSnapshot,
    SleevePosition,
    SleeveTarget,
)
from investment_manager.portfolio.policy import ProductPayoffPolicy, SleeveRiskTemplate
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    PortfolioRiskPolicy,
    SleeveRiskProfile,
)
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 26, 3, tzinfo=UTC)
SPOT = InstrumentId.binance_spot(
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
)
PERPETUAL = InstrumentId(
    product=InstrumentProduct.USD_M_PERPETUAL,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    settlement_asset="USDT",
)


def _mapping_cohort(
    *,
    version: str = "linear-product-payoff-v1",
    instruments: tuple[InstrumentId, ...] = (SPOT, PERPETUAL),
) -> tuple[ProductPayoffMappingIdentity, ...]:
    return (
        ProductPayoffMappingIdentity(
            economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
            projection_version=version,
            instrument_keys=tuple(sorted(item.key for item in instruments)),
            maximum_rule_age_seconds=900,
        ),
    )


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


def _contract() -> ForecastContract:
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
    benchmark = tuple(
        ForecastBenchmarkProbability(
            bucket_id=item.bucket_id,
            probability=probability,
        )
        for item, probability in zip(
            buckets,
            (Decimal("0.25"), Decimal("0.5"), Decimal("0.25")),
            strict=True,
        )
    )
    values = {
        "contract_version": "btc-reference-v1",
        "outcome_family_id": "btc-reference-4h",
        "target": _target(SPOT),
        "outcome_buckets": buckets,
        "horizon_minutes": 240,
        "decision_slot_rule": "test",
        "evaluation_trigger": "test",
        "information_cutoff_rule": "test",
        "completion_deadline_seconds": 300,
        "minimum_remaining_horizon_minutes": 120,
        "entry_anchor_rule": "test",
        "cost_semantics_version": "test",
        "validity_minutes": 60,
        "validity_conditions": ("test",),
        "settlement_rule": "test",
        "forecast_benchmark": benchmark,
        "decision_benchmark": "test",
    }
    return ForecastContract.create(**values)


def _anchor(instrument: InstrumentId, price: str, ref: str) -> ForecastPriceAnchor:
    return ForecastPriceAnchor(
        instrument_id=instrument.key,
        price=Decimal(price),
        observed_at=NOW,
        available_at=NOW,
        quote_ref=ref,
    )


def _forecast(
    contract: ForecastContract,
    *,
    decision_slot_id: str | None = None,
) -> BaseForecast:
    slot_id = decision_slot_id or stable_id("slot", NOW.isoformat())
    available_at = NOW + timedelta(minutes=1)
    program_input = {"fixture": "product-payoff-source"}
    return BaseForecast(
        forecast_id=stable_id("base_forecast", slot_id, "behavior-v1"),
        contract_id=contract.contract_id,
        decision_slot_id=slot_id,
        producer_id="test",
        producer_behavior_id="behavior-v1",
        outcome_family_id=contract.outcome_family_id,
        target=contract.target,
        horizon_minutes=contract.horizon_minutes,
        cutoff_prices=(_anchor(SPOT, "100", "spot-cutoff"),),
        entry_prices=(
            _anchor(SPOT, "100", "spot-entry").model_copy(
                update={"available_at": available_at}
            ),
        ),
        information_cutoff_at=NOW,
        input_observed_at=NOW,
        available_at=available_at,
        valid_until=NOW + timedelta(minutes=30),
        outcome_probabilities=(
            ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.2")),
            ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.3")),
            ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.5")),
        ),
        expected_gross_bps=Decimal("30"),
        input_refs=("test-input",),
        program_input_json=canonical_json(program_input),
        program_input_hash=content_hash(program_input),
    )


def _persisted_forecast(engine, contract: ForecastContract) -> BaseForecast:
    contracts = SqlForecastContractStore(engine)
    forecasts = SqlForecastStore(engine)
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=(_anchor(SPOT, "100", "spot-cutoff"),),
    )
    binding = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.PROGRAM.value,
            "test",
            "behavior-v1",
            ForecastPermission.CAPITAL_CANDIDATE.value,
            (),
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="test",
        producer_behavior_id="behavior-v1",
        permission=ForecastPermission.CAPITAL_CANDIDATE,
    )
    contracts.record_contract(contract)
    contracts.record_binding(binding, activated_at=NOW)
    contracts.record_slot(slot, binding=binding)
    forecast = _forecast(contract, decision_slot_id=slot.slot_id)
    assert forecasts.record(forecast)
    return forecast


def test_producer_panel_reader_preserves_the_complete_slot_obligation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    contract = _contract()
    forecast = _persisted_forecast(engine, contract)

    ledger = SqlProducerPanelReader(engine).read(
        producer_behavior_id=forecast.producer_behavior_id,
        as_of=forecast.available_at,
    )

    assert ledger.obligated_panel_count == 1
    assert ledger.pending_panel_count == 0
    assert len(ledger.complete_panels) == 1
    panel = ledger.complete_panels[0]
    assert panel.available_at == forecast.available_at
    assert panel.forecasts == (forecast,)
    assert panel.no_estimates == ()
    assert panel.obligations[0].slot_id == forecast.decision_slot_id


def _state(
    *,
    instrument: InstrumentId,
    direction: ExposureDirection,
    entry: str,
    exit_basis: str = "0",
    funding: str = "0",
    uncertainty: str = "0",
    margin: str = "1",
    available_at: datetime | None = None,
) -> ProductProjectionState:
    available_at = available_at or NOW + timedelta(minutes=1)
    return ProductProjectionState(
        target=_target(instrument, direction),
        entry_anchor=_anchor(instrument, entry, f"{instrument.product.value}-entry").model_copy(
            update={"available_at": available_at}
        ),
        valid_until=available_at + timedelta(minutes=5),
        expected_exit_basis_bps=Decimal(exit_basis),
        expected_funding_bps=Decimal(funding),
        mapping_uncertainty_bps=Decimal(uncertainty),
        initial_margin_fraction=Decimal(margin),
        product_rule_refs=(f"rules:{instrument.key}",),
        input_refs=(f"state:{instrument.key}",),
    )


def test_spot_projection_preserves_the_reference_distribution() -> None:
    contract = _contract()
    result = project_product_payoff(
        contract=contract,
        forecast=_forecast(contract),
        state=_state(
            instrument=SPOT,
            direction=ExposureDirection.LONG,
            entry="100",
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
    )

    assert tuple(item.payoff_bps for item in result.outcome_payoffs) == (
        Decimal("-100"),
        Decimal("0"),
        Decimal("100"),
    )
    assert tuple(item.conservative_payoff_bps for item in result.outcome_payoffs) == (
        Decimal("-100"),
        Decimal("0"),
        Decimal("100"),
    )
    assert result.expected_gross_bps == Decimal("30")
    assert result.conservative_gross_bps == Decimal("30")
    assert result.entry_basis_bps == 0


def test_perpetual_long_and_short_share_one_distribution_but_have_distinct_payoffs() -> None:
    contract = _contract()
    forecast = _forecast(contract)
    common = {
        "contract": contract,
        "forecast": forecast,
        "economic_exposure_id": "CRYPTO_NETWORK:BTC:USDT",
        "projection_version": "linear-product-payoff-v1",
    }
    long = project_product_payoff(
        **common,
        state=_state(
            instrument=PERPETUAL,
            direction=ExposureDirection.LONG,
            entry="100.1",
            funding="2",
            uncertainty="8",
            margin="0.1",
        ),
    )
    short = project_product_payoff(
        **common,
        state=_state(
            instrument=PERPETUAL,
            direction=ExposureDirection.SHORT,
            entry="99.9",
            funding="2",
            uncertainty="8",
            margin="0.1",
        ),
    )

    assert long.source_forecast_id == short.source_forecast_id == forecast.forecast_id
    assert long.target != short.target
    assert long.expected_gross_bps < Decimal("30")
    assert short.expected_gross_bps < Decimal("-30")
    assert long.conservative_gross_bps == long.expected_gross_bps - Decimal("8")
    assert short.conservative_gross_bps == short.expected_gross_bps - Decimal("8")
    assert all(
        item.payoff_bps - item.conservative_payoff_bps == Decimal("8")
        for item in (*long.outcome_payoffs, *short.outcome_payoffs)
    )


def test_derivative_projection_rejects_unquantified_mapping_uncertainty() -> None:
    with pytest.raises(ValueError, match="映射不确定性"):
        _state(
            instrument=PERPETUAL,
            direction=ExposureDirection.LONG,
            entry="100",
            uncertainty="0",
            margin="0.1",
        )


def test_projection_validates_the_forward_envelope_at_decimal_precision_boundary() -> None:
    contract = _contract()
    forecast = _forecast(contract)
    uncertainty = Decimal("9.987654321098765432109876543")

    projection = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=PERPETUAL,
            direction=ExposureDirection.LONG,
            entry="100.1",
            funding="2",
            uncertainty=str(uncertainty),
            margin="0.1",
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
    )

    assert all(
        item.conservative_payoff_bps == item.payoff_bps - uncertainty
        for item in projection.outcome_payoffs
    )
    # Reverse subtraction suffers cancellation at Decimal's fixed precision;
    # it cannot define whether the forward conservative envelope is correct.
    assert any(
        item.payoff_bps - item.conservative_payoff_bps != uncertainty
        for item in projection.outcome_payoffs
    )


def _decision_projection_inputs(forecast: BaseForecast | None = None):
    contract = _contract()
    forecast = forecast or _forecast(contract)
    projections = tuple(
        project_product_payoff(
            contract=contract,
            forecast=forecast,
            state=_state(
                instrument=instrument,
                direction=direction,
                entry=entry,
                uncertainty=uncertainty,
                margin=margin,
                available_at=forecast.available_at,
            ),
            economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
            projection_version="linear-product-payoff-v1",
        )
        for instrument, direction, entry, uncertainty, margin in (
            (SPOT, ExposureDirection.LONG, "100", "0", "1"),
            (PERPETUAL, ExposureDirection.LONG, "99.8", "1", "0.1"),
            (PERPETUAL, ExposureDirection.SHORT, "99.8", "1", "0.1"),
        )
    )
    authorization = CandidateCapitalAuthorization(
        version="candidate-v1",
        producer_id=forecast.producer_id,
        producer_behavior_id=forecast.producer_behavior_id,
        outcome_family_id=forecast.outcome_family_id,
        hypothesis_fingerprint="a" * 64,
    )
    sleeves = tuple(
        sorted(
            (
                PortfolioSleeveInput(
                    sleeve_id=SleeveTarget.identity_for(
                        portfolio_id="primary",
                        forecast_family=forecast.outcome_family_id,
                        forecast_target_id=projection.target.target_id,
                    ),
                    forecast=forecast,
                    payoff_projection=projection,
                    capital_authorization=authorization,
                )
                for projection in projections
            ),
            key=lambda item: item.sleeve_id,
        )
    )
    at = forecast.available_at
    quotes = tuple(
        sorted(
            (
                ExecutableQuote(
                    source_quote_id="spot-executable",
                    instrument=SPOT,
                    as_of=at,
                    observed_at=at,
                    bid=Decimal("100"),
                    bid_quantity=Decimal("100"),
                    ask=Decimal("100"),
                    ask_quantity=Decimal("100"),
                    source="test",
                ),
                ExecutableQuote(
                    source_quote_id="perpetual-executable",
                    instrument=PERPETUAL,
                    as_of=at,
                    observed_at=at,
                    bid=Decimal("99.8"),
                    bid_quantity=Decimal("100"),
                    ask=Decimal("99.8"),
                    ask_quantity=Decimal("100"),
                    source="test",
                ),
            ),
            key=lambda item: item.instrument.key,
        )
    )
    specs = tuple(
        sorted(
            (
                InstrumentExecutionSpec(
                    instrument=SPOT,
                    quantity_step=Decimal("0.00001"),
                    minimum_order_notional=Decimal("5"),
                    fee_bps=Decimal("1"),
                ),
                InstrumentExecutionSpec(
                    instrument=PERPETUAL,
                    quantity_step=Decimal("0.001"),
                    minimum_order_notional=Decimal("50"),
                    fee_bps=Decimal("0.5"),
                ),
            ),
            key=lambda item: item.instrument.key,
        )
    )
    return forecast, projections, sleeves, quotes, specs


def _decision_account(
    *,
    at: datetime,
    cycle_id: str,
    holding: PortfolioSleeveInput | None = None,
    holding_quantity: Decimal = Decimal("10"),
) -> PortfolioAccountSnapshot:
    positions = ()
    sleeve_positions = ()
    if holding is not None:
        position = InstrumentPosition(
            instrument=SPOT,
            quantity=holding_quantity,
            average_price=Decimal("100"),
        )
        positions = (position,)
        sleeve_positions = (
            SleevePosition(
                sleeve_id=holding.sleeve_id,
                forecast_family=holding.forecast.outcome_family_id,
                target=holding.target,
                legs=(position,),
            ),
        )
    return PortfolioAccountSnapshot(
        snapshot_id="account",
        cycle_id=cycle_id,
        portfolio_id="primary",
        as_of=at,
        observed_at=at,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
        positions=positions,
        sleeves=sleeve_positions,
    )


def _decision_engine() -> PortfolioDecisionEngine:
    return PortfolioDecisionEngine(
        PortfolioDecisionPolicy(
            version="product-selection-v1",
            portfolio_id="primary",
            enabled=True,
        )
    )


def test_portfolio_selects_one_best_product_expression_for_one_forecast() -> None:
    forecast, projections, sleeves, quotes, specs = _decision_projection_inputs()

    target = _decision_engine().decide(
        cycle_id="decision-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="decision-cycle",
        ),
        sleeves=sleeves,
        quotes=quotes,
        execution_specs=specs,
    )

    assert target is not None
    assert len(target.sleeves) == 1
    selected = target.sleeves[0]
    assert selected.forecast_target == projections[1].target
    assert selected.payoff_projection_id == projections[1].projection_id
    assert selected.decision_gross_bps == projections[1].conservative_gross_bps
    assert target.candidate_evaluations is not None
    assert sum(item.desired_gross_notional > 0 for item in target.candidate_evaluations) == 1
    assert "ALTERNATIVE_PRODUCT_EXPRESSION_REJECTED" in target.reason_codes


def test_portfolio_scales_experimental_size_from_full_cost_downside() -> None:
    contract = _contract()
    forecast = _forecast(contract).model_copy(
        update={
            "outcome_probabilities": (
                ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.25")),
                ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.40")),
                ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.35")),
            ),
            "expected_gross_bps": Decimal("10"),
        }
    )
    forecast, projections, sleeves, quotes, specs = _decision_projection_inputs(forecast)
    spot_projection = projections[0]
    spot_sleeve = next(item for item in sleeves if item.payoff_projection == spot_projection)
    spot_quotes = tuple(item for item in quotes if item.instrument == SPOT)
    spot_specs = tuple(item for item in specs if item.instrument == SPOT)

    target = _decision_engine().decide(
        cycle_id="downside-sized-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="downside-sized-cycle",
        ),
        sleeves=(spot_sleeve,),
        quotes=spot_quotes,
        execution_specs=spot_specs,
    )

    assert target is not None and target.candidate_evaluations is not None
    candidate = target.candidate_evaluations[0]
    downside_second_moment = sum(
        (
            bucket.probability
            * min(
                bucket.conservative_payoff_bps - candidate.cost.total_bps,
                Decimal("0"),
            )
            ** 2
            for bucket in spot_projection.outcome_payoffs
        ),
        Decimal("0"),
    )
    expected = (
        Decimal("10000")
        * candidate.decision_net_bps
        / downside_second_moment.sqrt()
    )
    assert Decimal("0") < expected < Decimal("3000")
    assert abs(target.sleeves[0].desired_gross_notional - expected) < Decimal("1e-20")



def test_downside_sizing_cannot_turn_a_positive_holding_into_a_costly_reduction() -> None:
    contract = _contract()
    forecast = _forecast(contract).model_copy(
        update={
            "outcome_probabilities": (
                ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.25")),
                ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.476")),
                ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.274")),
            ),
            "expected_gross_bps": Decimal("2.4"),
        }
    )
    forecast, projections, sleeves, quotes, specs = _decision_projection_inputs(forecast)
    spot_projection = projections[0]
    spot_sleeve = next(item for item in sleeves if item.payoff_projection == spot_projection)
    spot_quotes = tuple(item for item in quotes if item.instrument == SPOT)
    spot_specs = tuple(item for item in specs if item.instrument == SPOT)

    target = _decision_engine().decide(
        cycle_id="retain-positive-holding-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="retain-positive-holding-cycle",
            holding=spot_sleeve,
        ),
        sleeves=(spot_sleeve,),
        quotes=spot_quotes,
        execution_specs=spot_specs,
    )

    assert target is not None and target.candidate_evaluations is not None
    sleeve = target.sleeves[0]
    candidate = target.candidate_evaluations[0]
    assert sleeve.desired_gross_notional == Decimal("1000")
    assert candidate.evaluation_gross_notional == sleeve.desired_gross_notional
    assert candidate.desired_gross_notional == sleeve.desired_gross_notional
    assert candidate.decision_gross_bps == sleeve.decision_gross_bps
    assert candidate.cost == sleeve.cost
    assert candidate.decision_net_bps == sleeve.decision_net_bps
    assert candidate.decision_net_bps > 0

def test_bearish_forecast_can_select_the_short_product_expression() -> None:
    contract = _contract()
    bullish = _forecast(contract)
    forecast = bullish.model_copy(
        update={
            "outcome_probabilities": (
                ForecastBucketProbability(
                    bucket_id="LOSS",
                    probability=Decimal("0.7"),
                ),
                ForecastBucketProbability(
                    bucket_id="FLAT",
                    probability=Decimal("0.2"),
                ),
                ForecastBucketProbability(
                    bucket_id="GAIN",
                    probability=Decimal("0.1"),
                ),
            ),
            "expected_gross_bps": Decimal("-60"),
        }
    )
    forecast, projections, sleeves, quotes, specs = _decision_projection_inputs(
        forecast
    )

    target = _decision_engine().decide(
        cycle_id="bearish-decision-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="bearish-decision-cycle",
        ),
        sleeves=sleeves,
        quotes=quotes,
        execution_specs=specs,
    )

    assert target is not None and len(target.sleeves) == 1
    selected = target.sleeves[0]
    assert selected.forecast_target.legs[0].direction == ExposureDirection.SHORT
    short_projection = next(
        item
        for item in projections
        if item.target.legs[0].direction == ExposureDirection.SHORT
    )
    assert selected.payoff_projection_id == short_projection.projection_id


def test_product_switch_closes_the_old_expression_before_opening_the_new_one() -> None:
    forecast, projections, sleeves, quotes, specs = _decision_projection_inputs()
    spot = next(item for item in sleeves if item.target == projections[0].target)

    exit_target = _decision_engine().decide(
        cycle_id="switch-exit-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="switch-exit-cycle",
            holding=spot,
        ),
        sleeves=sleeves,
        quotes=quotes,
        execution_specs=specs,
    )

    assert exit_target is not None
    assert len(exit_target.sleeves) == 1
    assert exit_target.sleeves[0].sleeve_id == spot.sleeve_id
    assert exit_target.sleeves[0].desired_gross_notional == 0
    assert "PRODUCT_SWITCH_EXIT_FIRST" in exit_target.reason_codes

    open_target = _decision_engine().decide(
        cycle_id="switch-open-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="switch-open-cycle",
        ),
        sleeves=sleeves,
        quotes=quotes,
        execution_specs=specs,
    )
    assert open_target is not None
    assert len(open_target.sleeves) == 1
    assert open_target.sleeves[0].forecast_target == projections[1].target
    assert open_target.sleeves[0].desired_gross_notional > 0


def test_product_switch_uses_product_costs_without_a_portfolio_notional_gate() -> None:
    forecast, projections, sleeves, quotes, specs = _decision_projection_inputs()
    spot = next(item for item in sleeves if item.target == projections[0].target)

    target = _decision_engine().decide(
        cycle_id="small-switch-cycle",
        as_of=forecast.available_at,
        account=_decision_account(
            at=forecast.available_at,
            cycle_id="small-switch-cycle",
            holding=spot,
            holding_quantity=Decimal("0.1"),
        ),
        sleeves=sleeves,
        quotes=quotes,
        execution_specs=specs,
    )

    assert target is not None
    assert len(target.sleeves) == 1
    assert target.sleeves[0].sleeve_id == spot.sleeve_id
    assert target.sleeves[0].desired_gross_notional == 0
    assert "PRODUCT_SWITCH_EXIT_FIRST" in target.reason_codes
    assert "CASH_SELECTED_FOR_PRODUCT_TRANSITION" in target.reason_codes


def test_fresh_product_projection_after_source_entry_window_cannot_add_exposure() -> None:
    contract = _contract()
    forecast = _forecast(contract)
    decision_at = forecast.valid_until + timedelta(minutes=5)
    projection = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=SPOT,
            direction=ExposureDirection.LONG,
            entry="101",
            available_at=decision_at,
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
    )
    permission = CandidateCapitalAuthorization(
        version="candidate-v1",
        producer_id=forecast.producer_id,
        producer_behavior_id=forecast.producer_behavior_id,
        outcome_family_id=forecast.outcome_family_id,
        hypothesis_fingerprint="a" * 64,
    )
    sleeve = PortfolioSleeveInput(
        sleeve_id=SleeveTarget.identity_for(
            portfolio_id="primary",
            forecast_family=forecast.outcome_family_id,
            forecast_target_id=projection.target.target_id,
        ),
        forecast=forecast,
        payoff_projection=projection,
        capital_authorization=permission,
    )
    quote = ExecutableQuote(
        source_quote_id="late-spot",
        instrument=SPOT,
        as_of=decision_at,
        observed_at=decision_at,
        bid=Decimal("100.9"),
        bid_quantity=Decimal("100"),
        ask=Decimal("101"),
        ask_quantity=Decimal("100"),
        source="test",
    )
    spec = InstrumentExecutionSpec(
        instrument=SPOT,
        quantity_step=Decimal("0.00001"),
        minimum_order_notional=Decimal("5"),
        fee_bps=Decimal("1"),
    )

    target = _decision_engine().decide(
        cycle_id="late-entry-cycle",
        as_of=decision_at,
        account=_decision_account(at=decision_at, cycle_id="late-entry-cycle"),
        sleeves=(sleeve,),
        quotes=(quote,),
        execution_specs=(spec,),
    )

    assert target is not None
    assert target.sleeves == ()
    assert target.candidate_evaluations is not None
    assert not target.candidate_evaluations[0].forecast_current
    assert target.candidate_evaluations[0].desired_gross_notional == 0


def test_projection_store_is_idempotent_and_lists_products_by_target() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    contract = _contract()
    forecast = _persisted_forecast(engine, contract)
    cohort_id = _mapping_cohort()[0].cohort_id
    values = tuple(
        project_product_payoff(
            contract=contract,
            forecast=forecast,
            state=_state(
                instrument=instrument,
                direction=direction,
                entry="100",
                uncertainty=(
                    "0" if instrument.product == InstrumentProduct.SPOT else "5"
                ),
                margin=(
                    "1" if instrument.product == InstrumentProduct.SPOT else "0.1"
                ),
            ),
            economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
            projection_version="linear-product-payoff-v1",
            mapping_cohort_id=cohort_id,
        )
        for instrument, direction in (
            (SPOT, ExposureDirection.LONG),
            (PERPETUAL, ExposureDirection.LONG),
            (PERPETUAL, ExposureDirection.SHORT),
        )
    )
    store = SqlProductPayoffProjectionStore(engine)

    assert tuple(store.record(item) for item in values) == (True, True, True)
    assert tuple(store.record(item) for item in values) == (False, False, False)
    with pytest.raises(ValueError, match="已存在且内容不同"):
        store.record(values[0].model_copy(update={"mapping_cohort_id": "f" * 64}))
    assert store.get(values[0].projection_id) == values[0]
    assert set(store.for_source(forecast.forecast_id)) == set(values)

    later = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=SPOT,
            direction=ExposureDirection.LONG,
            entry="101",
            available_at=NOW + timedelta(minutes=10),
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
        mapping_cohort_id=cohort_id,
    )
    assert later.target == values[0].target
    assert later.projection_id != values[0].projection_id
    assert store.record(later)
    assert len(store.for_source(forecast.forecast_id)) == 4


def test_product_payoff_settlement_uses_executable_exit_and_actual_funding() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    contract = _contract()
    forecast = _persisted_forecast(engine, contract)
    projection = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=PERPETUAL,
            direction=ExposureDirection.LONG,
            entry="100.1",
            funding="1",
            uncertainty="5",
            margin="0.1",
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
        mapping_cohort_id=_mapping_cohort(instruments=(PERPETUAL,))[0].cohort_id,
    )
    store = SqlProductPayoffProjectionStore(engine)
    assert store.record(projection)
    market = InMemoryMarketDataStore()
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", PERPETUAL.key, 99),
            instrument=PERPETUAL,
            exchange_time=projection.evaluation_at,
            observed_at=projection.evaluation_at,
            bid=Decimal("101"),
            bid_quantity=Decimal("10"),
            ask=Decimal("101.1"),
            ask_quantity=Decimal("10"),
            update_id=99,
            source="test",
        )
    )
    funding_time = projection.projected_at + timedelta(hours=2)
    market.put_funding_settlement(
        FundingSettlement(
            settlement_id=stable_id(
                "funding_settlement",
                PERPETUAL.key,
                funding_time.isoformat(),
                FundingRateType.REGULAR.value,
            ),
            instrument=PERPETUAL,
            funding_time=funding_time,
            observed_at=funding_time + timedelta(seconds=1),
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("100.5"),
            rate_type=FundingRateType.REGULAR,
            source="test",
        )
    )
    settler = ProductPayoffOutcomeSettler(
        market=market,
        store=store,
        evaluation_version="product-payoff-outcome-v1",
        maximum_spot_age_seconds=300,
        maximum_perpetual_age_seconds=300,
        maximum_funding_gap_hours=8,
        settlement_grace_minutes=5,
    )

    result = settler.settle(as_of=projection.evaluation_at + timedelta(minutes=1))

    assert result.settled == 1
    assert result.outcome_unavailable == result.pending == 0
    outcome = store.outcome(
        stable_id(
            "product_payoff_outcome",
            projection.projection_id,
            "product-payoff-outcome-v1",
        )
    )
    assert outcome is not None
    assert outcome.status == ForecastOutcomeStatus.SETTLED
    assert outcome.leg is not None
    assert outcome.leg.exit_price == Decimal("101")
    assert outcome.leg.funding_settlement_ids
    assert outcome.realized_gross_bps == (
        outcome.leg.price_return_bps + outcome.leg.funding_return_bps
    )
    source_outcome_version = "forecast-target-outcome-v1"
    source_outcome = ForecastOutcome(
        outcome_id=stable_id(
            "forecast_outcome",
            forecast.decision_slot_id,
            source_outcome_version,
        ),
        contract_id=forecast.contract_id,
        decision_slot_id=forecast.decision_slot_id,
        evaluation_version=source_outcome_version,
        status=ForecastOutcomeStatus.SETTLED,
        information_cutoff_at=forecast.information_cutoff_at,
        outcome_start_at=None,
        evaluation_at=projection.evaluation_at,
        settled_at=projection.evaluation_at + timedelta(minutes=1),
        legs=(
            ForecastLegOutcome(
                instrument_id=SPOT.key,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
                reference_price=Decimal("100"),
                exit_price=Decimal("101"),
                price_return_bps=Decimal("100"),
            ),
        ),
        gross_target_return_bps=Decimal("100"),
        realized_bucket_id="GAIN",
        reason_code="TEST",
    )
    assert SqlForecastStore(engine).record_outcome(source_outcome)
    assert store.projection_outcomes(
        projection_ids=(projection.projection_id,),
        evaluation_version="product-payoff-outcome-v1",
    ) == ((projection, outcome),)
    assert store.projection_outcomes(
        projection_ids=(projection.projection_id,),
        evaluation_version="different-version",
    ) == ()
    with pytest.raises(ValueError, match="必须唯一且排序"):
        store.projection_outcomes(
            projection_ids=(projection.projection_id, projection.projection_id),
            evaluation_version="product-payoff-outcome-v1",
        )
    assert len(
        store.outcome_cases(
            product_outcome_version="product-payoff-outcome-v1",
            forecast_outcome_version=source_outcome_version,
            producer_behavior_id=forecast.producer_behavior_id,
            mapping_cohort=_mapping_cohort(instruments=(PERPETUAL,)),
        )
    ) == 1
    assert store.outcome_cases(
        product_outcome_version="product-payoff-outcome-v1",
        forecast_outcome_version=source_outcome_version,
        producer_behavior_id=forecast.producer_behavior_id,
        mapping_cohort=_mapping_cohort(
            version="retired-product-payoff-v0",
            instruments=(PERPETUAL,),
        ),
    ) == ()
    assert store.outcome_cases(
        product_outcome_version="product-payoff-outcome-v1",
        forecast_outcome_version="forecast-target-outcome-v1",
        producer_behavior_id="different-behavior",
        mapping_cohort=_mapping_cohort(),
    ) == ()
    assert settler.settle(
        as_of=projection.evaluation_at + timedelta(minutes=2)
    ).settled == 0
    pending_projection = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=PERPETUAL,
            direction=ExposureDirection.SHORT,
            entry="99.9",
            funding="1",
            uncertainty="5",
            margin="0.1",
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
        mapping_cohort_id=_mapping_cohort(instruments=(PERPETUAL,))[0].cohort_id,
    )
    assert store.record(pending_projection)
    assert store.outcome_cases(
        product_outcome_version="product-payoff-outcome-v1",
        forecast_outcome_version=source_outcome_version,
        producer_behavior_id=forecast.producer_behavior_id,
        mapping_cohort=_mapping_cohort(instruments=(PERPETUAL,)),
    ) == ()


def test_product_payoff_settlement_waits_for_grace_then_freezes_unavailable() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    contract = _contract()
    forecast = _persisted_forecast(engine, contract)
    projection = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=SPOT,
            direction=ExposureDirection.LONG,
            entry="100",
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
        mapping_cohort_id=_mapping_cohort(instruments=(SPOT,))[0].cohort_id,
    )
    store = SqlProductPayoffProjectionStore(engine)
    assert store.record(projection)
    settler = ProductPayoffOutcomeSettler(
        market=InMemoryMarketDataStore(),
        store=store,
        evaluation_version="product-payoff-outcome-v1",
        maximum_spot_age_seconds=300,
        maximum_perpetual_age_seconds=300,
        maximum_funding_gap_hours=8,
        settlement_grace_minutes=5,
    )

    waiting = settler.settle(as_of=projection.evaluation_at + timedelta(minutes=2))
    unavailable = settler.settle(
        as_of=projection.evaluation_at + timedelta(minutes=6)
    )

    assert waiting.pending == 1
    assert unavailable.outcome_unavailable == 1
    outcome = store.outcome(
        stable_id(
            "product_payoff_outcome",
            projection.projection_id,
            "product-payoff-outcome-v1",
        )
    )
    assert outcome is not None
    assert outcome.status == ForecastOutcomeStatus.OUTCOME_UNAVAILABLE


def test_product_payoff_evidence_removes_the_realized_economic_return() -> None:
    contract = _contract()
    forecast = _forecast(contract)
    projections = tuple(
        project_product_payoff(
            contract=contract,
            forecast=forecast,
            state=_state(
                instrument=instrument,
                direction=direction,
                entry=entry,
                uncertainty=uncertainty,
                margin=margin,
            ),
            economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
            projection_version="linear-product-payoff-v1",
            mapping_cohort_id=_mapping_cohort()[0].cohort_id,
        )
        for instrument, direction, entry, uncertainty, margin in (
            (SPOT, ExposureDirection.LONG, "100", "0", "1"),
            (PERPETUAL, ExposureDirection.LONG, "99.8", "1", "0.1"),
            (PERPETUAL, ExposureDirection.SHORT, "100.2", "1", "0.1"),
        )
    )
    first, second, short = projections
    later = project_product_payoff(
        contract=contract,
        forecast=forecast,
        state=_state(
            instrument=first.target.legs[0].instrument,
            direction=first.target.legs[0].direction,
            entry="102",
            uncertainty=(
                "0"
                if first.target.legs[0].instrument.product == InstrumentProduct.SPOT
                else "1"
            ),
            margin=(
                "1"
                if first.target.legs[0].instrument.product == InstrumentProduct.SPOT
                else "0.1"
            ),
            available_at=forecast.available_at + timedelta(minutes=5),
        ),
        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
        projection_version="linear-product-payoff-v1",
        mapping_cohort_id=_mapping_cohort()[0].cohort_id,
    )

    def outcome(projection, realized: Decimal) -> ProductPayoffOutcome:
        leg = ForecastLegOutcome(
            instrument_id=projection.target.legs[0].instrument.key,
            direction=projection.target.legs[0].direction,
            gross_weight=Decimal("1"),
            reference_price=projection.entry_anchor.price,
            exit_price=projection.entry_anchor.price * (
                Decimal("1") + realized / Decimal("10000")
            ),
            price_return_bps=realized,
            funding_return_bps=Decimal("0"),
            funding_settlement_ids=(),
        )
        version = "product-payoff-outcome-v1"
        return ProductPayoffOutcome(
            outcome_id=stable_id(
                "product_payoff_outcome",
                projection.projection_id,
                version,
            ),
            projection_id=projection.projection_id,
            source_forecast_id=projection.source_forecast_id,
            evaluation_version=version,
            status=ForecastOutcomeStatus.SETTLED,
            projected_at=projection.projected_at,
            evaluation_at=projection.evaluation_at,
            settled_at=projection.evaluation_at + timedelta(minutes=1),
            leg=leg,
            realized_gross_bps=realized,
            reason_code="TEST",
        )

    forecast_outcome_version = "forecast-target-outcome-v1"
    source_realized = Decimal("-50")
    source_anchor = forecast.entry_prices[0]
    source_outcome = ForecastOutcome(
        outcome_id=stable_id(
            "forecast_outcome",
            forecast.decision_slot_id,
            forecast_outcome_version,
        ),
        contract_id=forecast.contract_id,
        decision_slot_id=forecast.decision_slot_id,
        evaluation_version=forecast_outcome_version,
        status=ForecastOutcomeStatus.SETTLED,
        information_cutoff_at=forecast.information_cutoff_at,
        outcome_start_at=forecast.available_at,
        evaluation_at=forecast.economic_horizon_end,
        settled_at=forecast.economic_horizon_end + timedelta(minutes=1),
        legs=(
            ForecastLegOutcome(
                instrument_id=source_anchor.instrument_id,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
                reference_price=source_anchor.price,
                exit_price=source_anchor.price
                * (Decimal("1") + source_realized / Decimal("10000")),
                price_return_bps=source_realized,
            ),
        ),
        gross_target_return_bps=source_realized,
        realized_bucket_id="LOSS",
        reason_code="TEST",
    )
    realized_residuals = (Decimal("7"), Decimal("-2"), Decimal("3"))
    product_realized = (
        source_realized + realized_residuals[0],
        source_realized + realized_residuals[1],
        -source_realized + realized_residuals[2],
    )
    cases = (
        *(
            ProductPayoffEvaluationCase(
                source_forecast=forecast,
                source_outcome=source_outcome,
                projection=projection,
                product_outcome=outcome(projection, realized),
            )
            for projection, realized in zip(
                (first, second, short), product_realized, strict=True
            )
        ),
        ProductPayoffEvaluationCase(
            source_forecast=forecast,
            source_outcome=source_outcome,
            projection=later,
            product_outcome=outcome(later, Decimal("500")),
        ),
    )

    evidence = evaluate_product_payoff_evidence(
        cases,
        mapping_cohort=_mapping_cohort(),
        product_outcome_version="product-payoff-outcome-v1",
        forecast_outcome_version=forecast_outcome_version,
    )

    assert evidence.status == ProductPayoffEvidenceStatus.OBSERVED
    assert evidence.evaluation_version == "product-payoff-residual-evidence-v4"
    assert evidence.mapping_cohort == _mapping_cohort()
    assert evidence.source_forecast_count == 1
    assert evidence.settled_product_count == 4
    assert evidence.non_overlapping_panel_count == 1
    with pytest.raises(ValueError, match="评价输入身份不一致"):
        evaluate_product_payoff_evidence(
            cases,
            mapping_cohort=_mapping_cohort(version="retired-product-payoff-v0"),
            product_outcome_version="product-payoff-outcome-v1",
            forecast_outcome_version=forecast_outcome_version,
        )
    expected_residuals = (
        first.expected_gross_bps - forecast.expected_gross_bps,
        second.expected_gross_bps - forecast.expected_gross_bps,
        short.expected_gross_bps + forecast.expected_gross_bps,
    )
    assert evidence.mean_absolute_mapping_error_bps == sum(
        (
            abs(realized - expected)
            for realized, expected in zip(realized_residuals, expected_residuals, strict=True)
        ),
        Decimal("0"),
    ) / 3
    conservative_residuals = (
        first.conservative_gross_bps - forecast.expected_gross_bps,
        second.conservative_gross_bps - forecast.expected_gross_bps,
        short.conservative_gross_bps + forecast.expected_gross_bps,
    )
    assert evidence.mapping_conservative_coverage == Decimal(
        sum(
            realized >= conservative
            for realized, conservative in zip(
                realized_residuals, conservative_residuals, strict=True
            )
        )
    ) / 3
    assert evidence.mapping_residual_sign_accuracy == Decimal(
        sum(
            (realized > 0) - (realized < 0) == (expected > 0) - (expected < 0)
            for realized, expected in zip(
                realized_residuals, expected_residuals, strict=True
            )
        )
    ) / 3

    other_forecast = forecast.model_copy(
        update={
            "forecast_id": "joint-panel-other-forecast",
            "decision_slot_id": "joint-panel-other-slot",
            "outcome_family_id": "joint-panel-other-family",
        }
    )
    other_source_outcome = source_outcome.model_copy(
        update={
            "outcome_id": "joint-panel-other-source-outcome",
            "decision_slot_id": other_forecast.decision_slot_id,
        }
    )
    other_projection = first.model_copy(
        update={
            "projection_id": "joint-panel-other-projection",
            "source_forecast_id": other_forecast.forecast_id,
        }
    )
    other_realized_residual = Decimal("100")
    other_case = ProductPayoffEvaluationCase(
        source_forecast=other_forecast,
        source_outcome=other_source_outcome,
        projection=other_projection,
        product_outcome=outcome(
            other_projection,
            source_realized + other_realized_residual,
        ),
    )

    joint_evidence = evaluate_product_payoff_evidence(
        (*cases, other_case),
        mapping_cohort=_mapping_cohort(),
        product_outcome_version="product-payoff-outcome-v1",
        forecast_outcome_version=forecast_outcome_version,
    )
    other_expected_residual = (
        other_projection.expected_gross_bps - other_forecast.expected_gross_bps
    )
    assert joint_evidence.source_forecast_count == 2
    assert joint_evidence.non_overlapping_panel_count == 1
    assert joint_evidence.mean_absolute_mapping_error_bps == (
        sum(
            (
                abs(realized - expected)
                for realized, expected in zip(
                    realized_residuals,
                    expected_residuals,
                    strict=True,
                )
            ),
            Decimal("0"),
        )
        + abs(other_realized_residual - other_expected_residual)
    ) / 4


class _ProjectionStore:
    def __init__(self) -> None:
        self.values = []

    def record(self, projection) -> bool:
        self.values.append(projection)
        return True

    def for_source(self, source_forecast_id):
        return tuple(
            item
            for item in self.values
            if item.source_forecast_id == source_forecast_id
        )


class _TargetStates:
    def __init__(self) -> None:
        self.derivative_states = (
            SimpleNamespace(
                asset="BTC",
                market_symbol="BTCUSDT",
                evidence_ref="d" * 64,
                mark_index_premium_bps=Decimal("2"),
                perpetual_spread_bps=Decimal("1"),
                last_funding_rate_bps=Decimal("1"),
                trailing_funding_rate_mean_bps=Decimal("0.5"),
                trailing_funding_rate_stddev_bps=Decimal("0.25"),
                spot_mid_range_bps=Decimal("1"),
                reference_spot_mid_deviation_bps=Decimal("0.25"),
            ),
        )

    def build(self, *, as_of):
        return SimpleNamespace(
            input_refs=("target-state",),
            derivative_states=self.derivative_states,
        )


def _product_rules(
    observed_at: datetime,
    *,
    funding_override: bool = True,
) -> PerpetualProductRules:
    values = {
        "instrument": PERPETUAL,
        "observed_at": observed_at,
        "contract_type": "PERPETUAL",
        "status": "TRADING",
        "margin_asset": "USDT",
        "tick_size": Decimal("0.1"),
        "market_min_quantity": Decimal("0.001"),
        "market_max_quantity": Decimal("120"),
        "market_quantity_step": Decimal("0.001"),
        "minimum_notional": Decimal("50"),
        "funding_override_present": funding_override,
        "funding_interval_hours": 8 if funding_override else None,
        "adjusted_funding_rate_cap": (
            Decimal("0.003") if funding_override else None
        ),
        "adjusted_funding_rate_floor": (
            Decimal("-0.003") if funding_override else None
        ),
        "source": "test",
    }
    pending = PerpetualProductRules.model_construct(rules_id="pending", **values)
    return PerpetualProductRules(
        rules_id=stable_id(
            "perpetual_product_rules",
            PERPETUAL.key,
            observed_at.isoformat(),
            perpetual_product_rule_content_hash(pending),
        ),
        **values,
    )


def _put_context_market(
    market: InMemoryMarketDataStore,
    *,
    at: datetime,
    update_id: int,
    include_spot: bool = True,
    next_funding_time: datetime | None = None,
) -> None:
    if include_spot:
        market.put_quote(
            MarketQuote(
                quote_id=f"spot-product-{update_id}",
                symbol="BTCUSDT",
                observed_at=at,
                bid=Decimal("99.9"),
                bid_quantity=Decimal("10"),
                ask=Decimal("100"),
                ask_quantity=Decimal("10"),
                source="test",
            )
        )
    exchange_time = at - timedelta(seconds=1)
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", PERPETUAL.key, update_id),
            instrument=PERPETUAL,
            exchange_time=exchange_time,
            observed_at=at,
            bid=Decimal("99.9"),
            bid_quantity=Decimal("10"),
            ask=Decimal("100.1"),
            ask_quantity=Decimal("10"),
            update_id=update_id,
            source="test",
        )
    )
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                PERPETUAL.key,
                exchange_time.isoformat(),
            ),
            instrument=PERPETUAL,
            exchange_time=exchange_time,
            observed_at=at,
            mark_price=Decimal("100.02"),
            index_price=Decimal("100"),
            last_funding_rate=Decimal("0.0001"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=next_funding_time or at + timedelta(hours=2),
            source="test",
        )
    )


def _context_projector_fixture(
    *,
    rule_observed_at: datetime | None = None,
    funding_override: bool = True,
    market_observed_at: datetime | None = None,
    next_funding_time: datetime | None = None,
    perpetual_only: bool = False,
):
    contract = _contract()
    forecast = _forecast(contract)
    at = market_observed_at or forecast.available_at
    market = InMemoryMarketDataStore()
    _put_context_market(
        market,
        at=at,
        update_id=7,
        next_funding_time=next_funding_time,
    )
    rules = _product_rules(
        rule_observed_at or at,
        funding_override=funding_override,
    )
    market.put_perpetual_product_rules(rules)
    all_specs = (
        InstrumentExecutionSpec(
            instrument=SPOT,
            quantity_step=Decimal("0.00001"),
            minimum_order_notional=Decimal("5"),
            fee_bps=Decimal("10"),
        ),
        InstrumentExecutionSpec(
            instrument=PERPETUAL,
            quantity_step=Decimal("0.001"),
            minimum_order_notional=Decimal("50"),
            fee_bps=Decimal("5"),
        ),
    )
    specs = all_specs[1:] if perpetual_only else all_specs
    instruments = (PERPETUAL,) if perpetual_only else (SPOT, PERPETUAL)
    store = _ProjectionStore()
    projector = ContextProductPayoffProjector(
        policy=ProductPayoffPolicy(
            version="btc-linear-product-payoff-v1",
            economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
            instrument_keys=tuple(item.key for item in instruments),
            maximum_rule_age_seconds=900,
        ),
        contract=contract,
        market=market,
        target_states=_TargetStates(),
        instruments=instruments,
        execution_specs=specs,
        risk=SleeveRiskTemplate(
            version="test",
            basis_stress_bps=Decimal("100"),
            funding_stress_bps=Decimal("30"),
            execution_stress_bps=Decimal("100"),
            derivative_initial_margin_fraction=Decimal("0.1"),
        ),
        maximum_quote_age_seconds=300,
        store=store,  # type: ignore[arg-type]
    )
    return forecast, market, rules, store, projector


def test_context_projector_emits_spot_and_both_legal_perpetual_directions() -> None:
    forecast, _, rules, store, projector = _context_projector_fixture()

    projections = projector.project(forecast, as_of=forecast.available_at)

    assert len(projections) == 3
    assert {item.target.legs[0].direction for item in projections} == {
        ExposureDirection.LONG,
        ExposureDirection.SHORT,
    }
    derivatives = tuple(
        item
        for item in projections
        if item.target.legs[0].instrument == PERPETUAL
    )
    assert len(derivatives) == 2
    assert all(rules.rules_id in item.product_rule_refs for item in derivatives)
    assert all(item.expected_funding_bps == Decimal("0.5") for item in derivatives)
    assert len(store.values) == 3


def test_context_projector_can_build_without_mutating_projection_ledger() -> None:
    forecast, _, _, store, projector = _context_projector_fixture()

    built = projector.build(forecast, as_of=forecast.available_at)

    assert len(built) == 3
    assert store.values == []
    assert projector.project(forecast, as_of=forecast.available_at) == built
    assert store.values == list(built)


def test_complete_producer_panel_advances_its_own_cost_aware_account(app_config) -> None:
    contract = _contract()
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=(_anchor(SPOT, "100", "panel-cutoff"),),
    )
    binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="test",
        producer_behavior_id="behavior-v1",
        permission=ForecastPermission.CAPITAL_CANDIDATE,
    )
    obligation = ForecastSlotObligation.create(slot=slot, binding=binding)
    source = _forecast(contract, decision_slot_id=slot.slot_id)
    forecast, _, _, _, _ = _decision_projection_inputs(source)
    market = InMemoryMarketDataStore()
    _put_context_market(market, at=forecast.available_at, update_id=7)
    projector = SimpleNamespace(
        build=lambda source_forecast, *, as_of: tuple(
            item
            for item in _decision_projection_inputs(source_forecast)[1]
            if item.target.legs[0].instrument == PERPETUAL
        ),
    )
    risk = SleeveRiskTemplate(
        version="logical-panel-risk-v1",
        basis_stress_bps=Decimal("100"),
        funding_stress_bps=Decimal("30"),
        execution_stress_bps=Decimal("100"),
        derivative_initial_margin_fraction=Decimal("0.1"),
    )
    replay = ProducerCapitalReplay(
        producer_behavior_id=forecast.producer_behavior_id,
        capital_policy=app_config.capital.model_copy(update={"enabled": True}),
        initial_cash=Decimal("10000"),
        market=market,
        product_payoffs_by_family={forecast.outcome_family_id: projector},
        sleeve_risk=risk,
    )
    panel = ProducerDecisionPanel(
        panel_id="test-panel",
        producer_id=forecast.producer_id,
        producer_behavior_id=forecast.producer_behavior_id,
        slot_as_of=forecast.information_cutoff_at,
        information_cutoff_at=forecast.information_cutoff_at,
        available_at=forecast.available_at,
        obligations=(obligation,),
        slots=(slot,),
        forecasts=(forecast,),
        no_estimates=(),
    )

    step = replay.advance(panel)

    assert step.target is not None
    assert step.target.candidate_evaluations is not None
    assert all(item.cost.total_bps > 0 for item in step.target.candidate_evaluations)
    assert step.account.accounting is not None
    assert replay.account.result().account == step.account

    ledger = ProducerPanelLedger(
        producer_behavior_id=forecast.producer_behavior_id,
        as_of=forecast.available_at,
        obligated_panel_count=1,
        complete_panels=(panel,),
        pending_panel_count=0,
    )
    projection_calls = []
    recorded_projection = _decision_projection_inputs(forecast)[1][0]
    recorder = ProducerProductProjectionRecorder(
        producer_behavior_ids=(forecast.producer_behavior_id,),
        panels=SimpleNamespace(read=lambda **_kwargs: ledger),
        product_payoffs_by_family={
            forecast.outcome_family_id: SimpleNamespace(
                project=lambda value, *, as_of: (
                    projection_calls.append((value.forecast_id, as_of))
                    or recorded_projection,
                )
            )
        },
    )

    first_recording = recorder.reconcile(as_of=forecast.available_at)
    second_recording = recorder.reconcile(as_of=forecast.available_at)

    assert first_recording.processed_panel_count == 1
    assert first_recording.projected_forecast_count == 1
    assert first_recording.projection_count == 1
    assert first_recording.unavailable_forecast_count == 0
    assert second_recording.processed_panel_count == 0
    assert projection_calls == [(forecast.forecast_id, panel.available_at)]

    def independent_replay() -> ProducerCapitalReplay:
        return ProducerCapitalReplay(
            producer_behavior_id=forecast.producer_behavior_id,
            capital_policy=app_config.capital.model_copy(update={"enabled": True}),
            initial_cash=Decimal("10000"),
            market=market,
            product_payoffs_by_family={forecast.outcome_family_id: projector},
            sleeve_risk=risk,
        )

    capital_path = evaluate_producer_capital_path(
        initial_cash=Decimal("10000"),
        ledger=ledger,
        replay=independent_replay(),
    )

    assert capital_path is not None
    assert capital_path.panel_ids == (panel.panel_id,)
    assert capital_path.included_strata == (
        ForecastSlotStratum.CADENCE_ONLY,
        ForecastSlotStratum.MATERIAL_STATE_ONLY,
    )
    assert capital_path.path.account == capital_path.steps[-1].account

    event_at = NOW + timedelta(minutes=5)
    event_cause = ForecastSlotCause.material_state(
        policy_version="test-material-v1",
        trigger_refs=("event-1",),
    )
    event_slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=event_at,
        cutoff_prices=(
            _anchor(SPOT, "100", "event-cutoff").model_copy(
                update={"observed_at": event_at, "available_at": event_at}
            ),
        ),
        cause=event_cause,
    )
    event_obligation = ForecastSlotObligation.create(slot=event_slot, binding=binding)
    event_available_at = event_at + timedelta(minutes=1)
    event_forecast = forecast.model_copy(
        update={
            "forecast_id": stable_id(
                "base_forecast", event_slot.slot_id, forecast.producer_behavior_id
            ),
            "decision_slot_id": event_slot.slot_id,
            "information_cutoff_at": event_at,
            "input_observed_at": event_at,
            "available_at": event_available_at,
            "valid_until": event_at + timedelta(minutes=30),
            "cutoff_prices": (
                forecast.cutoff_prices[0].model_copy(
                    update={"observed_at": event_at, "available_at": event_at}
                ),
            ),
            "entry_prices": (
                forecast.entry_prices[0].model_copy(
                    update={
                        "observed_at": event_available_at,
                        "available_at": event_available_at,
                    }
                ),
            ),
        }
    )
    event_panel = ProducerDecisionPanel(
        panel_id="event-panel",
        producer_id=event_forecast.producer_id,
        producer_behavior_id=event_forecast.producer_behavior_id,
        slot_as_of=event_at,
        information_cutoff_at=event_at,
        available_at=event_available_at,
        obligations=(event_obligation,),
        slots=(event_slot,),
        forecasts=(event_forecast,),
        no_estimates=(),
    )
    _put_context_market(market, at=event_available_at, update_id=8)
    event_ledger = ledger.model_copy(
        update={
            "as_of": event_available_at,
            "obligated_panel_count": 2,
            "complete_panels": (panel, event_panel),
        }
    )
    all_slots = evaluate_producer_capital_path(
        initial_cash=Decimal("10000"),
        ledger=event_ledger,
        replay=independent_replay(),
    )
    assert all_slots is not None
    cadence_only = evaluate_producer_capital_path(
        initial_cash=Decimal("10000"),
        ledger=event_ledger,
        replay=independent_replay(),
        allowed_strata=(ForecastSlotStratum.CADENCE_ONLY,),
        mark_at=all_slots.as_of,
    )

    assert cadence_only is not None
    assert all_slots.panel_ids == (panel.panel_id, event_panel.panel_id)
    assert cadence_only.panel_ids == (panel.panel_id,)
    assert cadence_only.included_strata == (ForecastSlotStratum.CADENCE_ONLY,)
    assert cadence_only.as_of == all_slots.as_of == event_available_at
    assert len(cadence_only.steps) == 1


def test_producer_logical_account_reuses_cost_after_capital_and_paper_execution(
    app_config,
) -> None:
    forecast, _, sleeves, quotes, _ = _decision_projection_inputs()
    perpetual_sleeves = tuple(
        item for item in sleeves if item.target.legs[0].instrument == PERPETUAL
    )
    perpetual_quotes = tuple(item for item in quotes if item.instrument == PERPETUAL)
    profiles = tuple(
        SleeveRiskProfile(
            sleeve_id=item.sleeve_id,
            version="logical-account-risk-v1",
            basis_stress_bps=Decimal("100"),
            funding_stress_bps=Decimal("30"),
            execution_stress_bps=Decimal("100"),
            derivative_initial_margin_fraction=Decimal("0.1"),
        )
        for item in perpetual_sleeves
    )
    evaluator = ProducerLogicalAccount(
        producer_behavior_id=forecast.producer_behavior_id,
        capital_policy=app_config.capital.model_copy(update={"enabled": True}),
        initial_cash=Decimal("10000"),
    )

    step = evaluator.advance(
        as_of=forecast.available_at,
        sleeves=perpetual_sleeves,
        quotes=perpetual_quotes,
        risk_profiles=profiles,
    )
    assert step.target is not None
    assert step.risk_decision is not None
    assert step.trade_plan is not None
    assert step.forecast_ids == (forecast.forecast_id,)
    assert len(step.execution_groups) == 1
    assert step.execution_groups[0].terminal
    assert len(step.account.positions) == 1
    assert step.account.positions[0].instrument == PERPETUAL
    assert step.account.accounting is not None
    assert step.account.accounting.fee_cost > 0
    assert step.account.equity == Decimal("10000") - step.account.accounting.fee_cost

    mark_at = forecast.available_at + timedelta(minutes=5)
    marked = evaluator.mark(
        as_of=mark_at,
        quotes=(
            perpetual_quotes[0].model_copy(
                update={
                    "source_quote_id": "perpetual-common-cutoff",
                    "as_of": mark_at,
                    "observed_at": mark_at,
                    "bid": Decimal("100.5"),
                    "ask": Decimal("100.5"),
                }
            ),
        ),
    )
    assert marked.as_of == mark_at
    assert marked.equity > step.account.equity

    second_at = forecast.available_at + timedelta(minutes=10)
    second_slot_id = stable_id("slot", second_at.isoformat())
    second_forecast = forecast.model_copy(
        update={
            "forecast_id": stable_id(
                "base_forecast",
                second_slot_id,
                forecast.producer_behavior_id,
            ),
            "decision_slot_id": second_slot_id,
            "information_cutoff_at": second_at - timedelta(minutes=1),
            "input_observed_at": second_at - timedelta(minutes=1),
            "available_at": second_at,
            "valid_until": second_at + timedelta(minutes=30),
            "cutoff_prices": (
                _anchor(SPOT, "101", "second-spot-cutoff").model_copy(
                    update={
                        "observed_at": second_at - timedelta(minutes=1),
                        "available_at": second_at - timedelta(minutes=1),
                    }
                ),
            ),
            "entry_prices": (
                _anchor(SPOT, "101", "second-spot-entry").model_copy(
                    update={"observed_at": second_at, "available_at": second_at}
                ),
            ),
            "outcome_probabilities": (
                ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.7")),
                ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.2")),
                ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.1")),
            ),
            "expected_gross_bps": Decimal("-60"),
        }
    )
    authorization = CandidateCapitalAuthorization(
        version="candidate-v1",
        producer_id=second_forecast.producer_id,
        producer_behavior_id=second_forecast.producer_behavior_id,
        outcome_family_id=second_forecast.outcome_family_id,
        hypothesis_fingerprint="a" * 64,
    )
    second_sleeves = tuple(
        sorted(
            (
                PortfolioSleeveInput(
                    sleeve_id=SleeveTarget.identity_for(
                        portfolio_id="primary",
                        forecast_family=second_forecast.outcome_family_id,
                        forecast_target_id=projection.target.target_id,
                    ),
                    forecast=second_forecast,
                    payoff_projection=projection,
                    capital_authorization=authorization,
                )
                for projection in (
                    project_product_payoff(
                        contract=_contract(),
                        forecast=second_forecast,
                        state=_state(
                            instrument=PERPETUAL,
                            direction=direction,
                            entry="101",
                            uncertainty="1",
                            margin="0.1",
                            available_at=second_at,
                        ),
                        economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
                        projection_version="linear-product-payoff-v1",
                    )
                    for direction in (
                        ExposureDirection.LONG,
                        ExposureDirection.SHORT,
                    )
                )
            ),
            key=lambda item: item.sleeve_id,
        )
    )
    second_quote = perpetual_quotes[0].model_copy(
        update={
            "source_quote_id": "perpetual-second-executable",
            "as_of": second_at,
            "observed_at": second_at,
            "bid": Decimal("101"),
            "ask": Decimal("101"),
        }
    )
    second_profiles = tuple(
        item.model_copy(update={"sleeve_id": sleeve.sleeve_id})
        for sleeve, item in zip(second_sleeves, profiles, strict=True)
    )

    reversal = evaluator.advance(
        as_of=second_at,
        sleeves=second_sleeves,
        quotes=(second_quote,),
        risk_profiles=second_profiles,
    )
    result = evaluator.result()

    assert reversal.account.positions == ()
    assert reversal.trade_plan is not None
    assert len(reversal.trade_plan.groups) == 1
    assert reversal.trade_plan.groups[0].legs[0].reduce_only
    assert reversal.account.accounting is not None
    assert reversal.account.accounting.fee_cost > step.account.accounting.fee_cost
    assert result.account == reversal.account
    assert result.gross_turnover > 0
    assert result.step_ids == (step.step_id, reversal.step_id)


def test_context_projector_keeps_spot_reference_out_of_perpetual_only_candidates() -> None:
    forecast, _, rules, store, projector = _context_projector_fixture(
        perpetual_only=True
    )

    projections = projector.project(forecast, as_of=forecast.available_at)

    assert len(projections) == 2
    assert {item.target.legs[0].instrument for item in projections} == {PERPETUAL}
    assert {item.target.legs[0].direction for item in projections} == {
        ExposureDirection.LONG,
        ExposureDirection.SHORT,
    }
    assert all(rules.rules_id in item.product_rule_refs for item in projections)
    assert len(store.values) == 2


def test_context_projector_advances_fresh_lagging_funding_schedule() -> None:
    forecast, _, rules, store, projector = _context_projector_fixture(
        market_observed_at=NOW,
        next_funding_time=NOW + timedelta(seconds=30),
    )

    projections = projector.project(forecast, as_of=forecast.available_at)

    derivatives = tuple(
        item
        for item in projections
        if item.target.legs[0].instrument == PERPETUAL
    )
    assert len(derivatives) == 2
    assert all(rules.rules_id in item.product_rule_refs for item in derivatives)
    assert all(item.expected_funding_bps == Decimal("0") for item in derivatives)
    assert len(store.values) == 3


def test_context_projector_rejects_derivative_after_rule_success_becomes_stale() -> None:
    forecast, _, _, store, projector = _context_projector_fixture(
        rule_observed_at=NOW - timedelta(minutes=14),
    )

    projections = projector.project(forecast, as_of=forecast.available_at)

    assert len(projections) == 1
    assert projections[0].target.legs[0].instrument == SPOT
    assert len(store.values) == 1


@pytest.mark.parametrize("invalid_input", ("mapping", "rules", "reference_quote"))
def test_held_perpetual_exits_when_current_product_projection_is_unavailable(
    invalid_input: str,
) -> None:
    forecast, market, _, _, projector = _context_projector_fixture()
    projected = projector.project(forecast, as_of=forecast.available_at)
    held_projection = next(
        item
        for item in projected
        if item.target.legs[0].instrument == PERPETUAL
        and item.target.legs[0].direction == ExposureDirection.LONG
    )
    if invalid_input == "mapping":
        decision_at = forecast.available_at + timedelta(minutes=1)
        projector.target_states.derivative_states = ()
        assert decision_at < held_projection.valid_until
    else:
        decision_at = forecast.available_at + timedelta(
            minutes=16 if invalid_input == "rules" else 6
        )
        _put_context_market(
            market,
            at=decision_at,
            update_id=8,
            include_spot=invalid_input == "rules",
        )

    authorization = CandidateCapitalAuthorization(
        version="candidate-v1",
        producer_id=forecast.producer_id,
        producer_behavior_id=forecast.producer_behavior_id,
        outcome_family_id=forecast.outcome_family_id,
        hypothesis_fingerprint="a" * 64,
    )
    source = SimpleNamespace(
        contract=projector.contract,
        product_payoffs=projector,
        capital_authorization=authorization,
    )
    capital = CapitalCycleService.__new__(CapitalCycleService)
    capital._source_by_family = {forecast.outcome_family_id: source}
    capital._forecasts = SimpleNamespace(
        latest_capital_base_for_target=lambda **_: forecast,
    )
    sleeve_id = SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family=forecast.outcome_family_id,
        forecast_target_id=held_projection.target.target_id,
    )
    instrument_position = InstrumentPosition(
        instrument=PERPETUAL,
        quantity=Decimal("10"),
        average_price=Decimal("100"),
    )
    position = SleevePosition(
        sleeve_id=sleeve_id,
        forecast_family=forecast.outcome_family_id,
        target=held_projection.target,
        legs=(instrument_position,),
    )

    sleeve = capital._position_sleeve_input(
        position=position,
        as_of=decision_at,
    )

    assert sleeve.payoff_projection == held_projection
    assert not sleeve.payoff_projection_current
    quote = ExecutableQuote(
        source_quote_id="perpetual-exit",
        instrument=PERPETUAL,
        as_of=decision_at,
        observed_at=decision_at,
        bid=Decimal("99.9"),
        bid_quantity=Decimal("100"),
        ask=Decimal("100.1"),
        ask_quantity=Decimal("100"),
        source="test",
    )
    account = PortfolioAccountSnapshot(
        snapshot_id="held-perpetual-account",
        cycle_id="held-perpetual-exit",
        portfolio_id="primary",
        as_of=decision_at,
        observed_at=decision_at,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
        positions=(instrument_position,),
        sleeves=(position,),
    )
    if invalid_input == "reference_quote":
        capital._policy = SimpleNamespace(
            decision=SimpleNamespace(portfolio_id="primary")
        )
        decision_sleeves = capital._decision_sleeves(
            forecasts=(forecast,),
            account=account,
            as_of=decision_at,
        )
        assert decision_sleeves == (sleeve,)
    target = _decision_engine().decide(
        cycle_id="held-perpetual-exit",
        as_of=decision_at,
        account=account,
        sleeves=(sleeve,),
        quotes=(quote,),
        execution_specs=(
            InstrumentExecutionSpec(
                instrument=PERPETUAL,
                quantity_step=Decimal("0.001"),
                minimum_order_notional=Decimal("50"),
                fee_bps=Decimal("0.5"),
            ),
        ),
    )

    assert target is not None
    assert target.sleeves[0].desired_gross_notional == 0
    assert "EXPIRED_FORECAST_EXIT" in target.reason_codes
    assert target.candidate_evaluations is not None
    assert target.candidate_evaluations[0].validity_reason_codes == (
        "PRODUCT_PAYOFF_INPUT_INVALID",
    )
    risk = PortfolioRiskEngine(
        PortfolioRiskPolicy(
            version="held-product-exit-v1",
            instrument_allowlist=(PERPETUAL.key,),
            maximum_quote_age_seconds=180,
            maximum_quote_skew_seconds=15,
            maximum_account_age_seconds=60,
            maximum_drawdown_fraction=Decimal("0.10"),
            maximum_gross_exposure_fraction=Decimal("0.50"),
            maximum_net_delta_fraction=Decimal("0.50"),
            maximum_instrument_fraction=Decimal("0.50"),
            maximum_margin_fraction=Decimal("0.50"),
            maximum_stress_loss_fraction=Decimal("0.10"),
            maximum_spread_bps=Decimal("20"),
            maximum_unhedged_fraction=Decimal("0.05"),
            maximum_unhedged_seconds=10,
            reduction_authorization_seconds=300,
        )
    ).evaluate(
        target=target,
        account=account,
        quotes=(quote,),
        risk_profiles=(
            SleeveRiskProfile(
                sleeve_id=sleeve_id,
                version="held-product-risk-v1",
                basis_stress_bps=Decimal("100"),
                funding_stress_bps=Decimal("30"),
                execution_stress_bps=Decimal("100"),
                derivative_initial_margin_fraction=Decimal("0.1"),
            ),
        ),
        as_of=decision_at,
    )
    assert risk.outcome == RiskOutcome.APPROVED
    assert risk.approved_target is not None
    plan = TradePlanner(
        TradePlannerPolicy(
            version="held-product-exit-v1",
            managed_instruments=(PERPETUAL.key,),
        )
    ).plan(
        approved=risk.approved_target,
        account=account,
        quotes=(quote,),
        specs=(
            InstrumentExecutionSpec(
                instrument=PERPETUAL,
                quantity_step=Decimal("0.001"),
                minimum_order_notional=Decimal("50"),
                fee_bps=Decimal("0.5"),
            ),
        ),
        as_of=decision_at,
    )
    assert len(plan.groups) == 1
    assert len(plan.groups[0].legs) == 1
    assert plan.groups[0].legs[0].side.value == "SELL"
    assert plan.groups[0].legs[0].reduce_only


def test_context_projector_fails_closed_without_override_or_stable_history() -> None:
    forecast, _, rules, store, projector = _context_projector_fixture(
        funding_override=False,
    )

    projections = projector.project(forecast, as_of=forecast.available_at)

    assert not rules.funding_override_present
    assert len(projections) == 1
    assert projections[0].target.legs[0].instrument == SPOT
    assert len(store.values) == 1


def test_context_projector_infers_standard_interval_without_funding_override() -> None:
    forecast, market, rules, store, projector = _context_projector_fixture(
        funding_override=False,
    )
    at = forecast.available_at
    settlements = tuple(
        FundingSettlement(
            settlement_id=stable_id(
                "funding_settlement",
                PERPETUAL.key,
                funding_at.isoformat(),
                FundingRateType.REGULAR.value,
            ),
            instrument=PERPETUAL,
            funding_time=funding_at,
            observed_at=funding_at + timedelta(minutes=1),
            funding_rate=Decimal("0.00005"),
            mark_price=Decimal("100"),
            rate_type=FundingRateType.REGULAR,
            source="test",
        )
        for funding_at in (
            at - timedelta(hours=24),
            at - timedelta(hours=16),
            at - timedelta(hours=8),
        )
    )
    for settlement in settlements:
        market.put_funding_settlement(settlement)

    projections = projector.project(forecast, as_of=at)

    derivatives = tuple(
        item
        for item in projections
        if item.target.legs[0].instrument == PERPETUAL
    )
    assert not rules.funding_override_present
    assert len(derivatives) == 2
    assert all(item.expected_funding_bps == Decimal("0.5") for item in derivatives)
    assert all(
        {item.settlement_id for item in settlements}.issubset(projection.input_refs)
        for projection in derivatives
    )
    assert len(store.values) == 3
