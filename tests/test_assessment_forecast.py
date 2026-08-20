from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

from investment_manager.analyst import AnalystResult
from investment_manager.forecast.calibration import (
    AssessmentCalibrationBuilder,
    AssessmentCalibrationBuildSpec,
)
from investment_manager.forecast.execution import (
    AssessmentExecutionStatus,
    ContextAssessmentExecutor,
)
from investment_manager.forecast.models import (
    AssessmentUncertainty,
    ContextAssessment,
    ContextView,
    DirectionalView,
    ForecastOutcomeStatus,
    ForecastRole,
    PricedState,
)
from investment_manager.forecast.outcomes import (
    AssessmentViewOutcome,
    AssessmentViewOutcomeSettler,
    SqlAssessmentViewOutcomeStore,
)
from investment_manager.forecast.projection import (
    AssessmentForecastPolicy,
    AssessmentForecastProjector,
    AssessmentViewCalibration,
    build_assessment_view_calibration,
)
from investment_manager.forecast.repository import SqlContextAssessmentStore
from investment_manager.forecast.tables import assessment_view_outcomes
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import MarketTrade
from investment_manager.market.repository import SqlMarketDataStore, create_market_schema
from investment_manager.schema import create_schema
from investment_manager.state.decision_packet import (
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
        rules_digest=("rule-v1",),
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


def _calibration(**updates):
    values = {
        "version": "assessment-calibration-v1",
        "analysis_scope": "crypto-portfolio",
        "analysis_behavior_hash": "b" * 64,
        "outcome_evaluation_version": "assessment-outcome-v1",
        "method_version": "mean-lower-bound-v1",
        "lower_confidence_z": Decimal("1.96"),
        "asset": "BTC",
        "symbol": "BTCUSDT",
        "horizon_minutes": 240,
        "direction": DirectionalView.UP,
        "already_priced": PricedState.PARTIAL,
        "uncertainty": AssessmentUncertainty.MEDIUM,
        "training_start": NOW - timedelta(days=30),
        "training_end": NOW - timedelta(days=2),
        "trained_through": NOW - timedelta(days=2),
        "available_at": NOW - timedelta(days=1),
        "expected_edge_half_life_seconds": 7_200,
        "expected_gross_bps": Decimal("18"),
        "conservative_gross_bps": Decimal("7"),
        "dispersion_bps": Decimal("24"),
        "sample_size": 60,
        "non_overlapping_sample_size": 35,
    }
    values.update(updates)
    values.setdefault(
        "source_refs",
        tuple(
            f"outcome-{index:02}" for index in range(values["sample_size"])
        ),
    )
    values.setdefault(
        "non_overlapping_source_refs",
        tuple(
            f"outcome-{index:02}"
            for index in range(values["non_overlapping_sample_size"])
        ),
    )
    return build_assessment_view_calibration(**values)


def _projector() -> AssessmentForecastProjector:
    return AssessmentForecastProjector(
        AssessmentForecastPolicy(
            version="assessment-forecast-v1",
            maximum_age_seconds=3_600,
            maximum_reference_market_age_seconds=30,
            minimum_sample_size=40,
            minimum_non_overlapping_sample_size=30,
        )
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


def test_exact_point_in_time_calibration_produces_ai_event_forecast() -> None:
    result = _projector().project(
        packet=_packet(),
        assessment=_assessment(),
        calibrations=(_calibration(),),
        reference_trades=(_reference_trade(),),
    )

    assert result.uncalibrated_views == ()
    assert len(result.forecasts) == 1
    forecast = result.forecasts[0]
    assert forecast.role == ForecastRole.AI_EVENT
    assert forecast.symbol == "BTCUSDT"
    assert forecast.reference_price == Decimal("70010")
    assert forecast.assessment_id == "assessment-1"
    assert forecast.base_forecast_id is None
    assert forecast.conservative_gross_bps == Decimal("7")
    assert forecast.valid_until == _assessment().available_at + timedelta(hours=1)


def test_missing_or_underpowered_calibration_cannot_grant_ai_capital_signal() -> None:
    missing = _projector().project(
        packet=_packet(),
        assessment=_assessment(),
        calibrations=(),
        reference_trades=(_reference_trade(),),
    )
    underpowered = _projector().project(
        packet=_packet(),
        assessment=_assessment(),
        calibrations=(_calibration(non_overlapping_sample_size=29),),
        reference_trades=(_reference_trade(),),
    )

    assert missing.forecasts == ()
    assert missing.uncalibrated_views == (("BTC", 240),)
    assert underpowered.forecasts == ()
    assert underpowered.uncalibrated_views == (("BTC", 240),)


def test_future_calibration_is_rejected_instead_of_leaking() -> None:
    with pytest.raises(ValueError, match="as_of 之后"):
        _projector().project(
            packet=_packet(),
            assessment=_assessment(),
            calibrations=(_calibration(available_at=NOW + timedelta(seconds=1)),),
            reference_trades=(_reference_trade(),),
        )


def test_other_analysis_behavior_calibration_is_rejected() -> None:
    with pytest.raises(ValueError, match="其他分析行为"):
        _projector().project(
            packet=_packet(),
            assessment=_assessment(),
            calibrations=(_calibration(analysis_behavior_hash="c" * 64),),
            reference_trades=(_reference_trade(),),
        )


def test_stale_reference_trade_cannot_hide_ai_latency() -> None:
    with pytest.raises(ValueError, match="过期"):
        _projector().project(
            packet=_packet(),
            assessment=_assessment(),
            calibrations=(_calibration(),),
            reference_trades=(
                _reference_trade(
                    event_time=NOW - timedelta(minutes=1),
                    observed_at=NOW - timedelta(minutes=1),
                ),
            ),
        )


def test_calibration_identity_is_content_addressed() -> None:
    calibration = _calibration()
    payload = calibration.model_dump()
    payload["expected_gross_bps"] = Decimal("99")

    with pytest.raises(ValueError, match="content_hash"):
        AssessmentViewCalibration.model_validate(payload)


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

    before_maturity = settler.settle(as_of=evaluation_at - timedelta(seconds=1))
    settled = settler.settle(as_of=evaluation_at + timedelta(seconds=1))
    replayed = settler.settle(as_of=evaluation_at + timedelta(seconds=2))

    assert before_maturity.pending == 1
    assert settled.settled == 1
    assert replayed.settled == 0
    with engine.connect() as connection:
        payload = connection.execute(
            select(assessment_view_outcomes.c.payload)
        ).scalar_one()
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
        payload = connection.execute(
            select(assessment_view_outcomes.c.payload)
        ).scalar_one()
    outcome = AssessmentViewOutcome.model_validate(payload)
    assert outcome.reference_price is None
    assert outcome.reason_code == (
        "REFERENCE_MARKET_DATA_MISSING_AT_ASSESSMENT_AVAILABILITY"
    )


def _settled_assessment_outcome(
    index: int,
    *,
    signal_at: datetime,
    directional_return_bps: Decimal,
) -> AssessmentViewOutcome:
    assessment_id = f"assessment-training-{index}"
    evaluation_at = signal_at + timedelta(minutes=240)
    reference_price = Decimal("100")
    exit_price = reference_price * (
        Decimal("1") + directional_return_bps / Decimal("10000")
    )
    return AssessmentViewOutcome(
        outcome_id=stable_id(
            "assessment_view_outcome",
            assessment_id,
            "BTC",
            240,
            "assessment-outcome-v1",
        ),
        assessment_id=assessment_id,
        decision_packet_hash="a" * 64,
        analysis_scope="crypto-portfolio",
        analysis_behavior_hash="b" * 64,
        evaluation_version="assessment-outcome-v1",
        asset="BTC",
        symbol="BTCUSDT",
        horizon_minutes=240,
        direction=DirectionalView.UP,
        already_priced=PricedState.PARTIAL,
        uncertainty=AssessmentUncertainty.MEDIUM,
        status=ForecastOutcomeStatus.SETTLED,
        signal_observed_at=signal_at,
        evaluation_at=evaluation_at,
        settled_at=evaluation_at + timedelta(minutes=1),
        reference_price=reference_price,
        exit_price=exit_price,
        exit_event_time=evaluation_at,
        market_return_bps=directional_return_bps,
        directional_return_bps=directional_return_bps,
        direction_correct=directional_return_bps > 0,
        reason_code="DIRECTIONAL_RETURN_AVAILABLE",
    )


def _calibration_spec() -> AssessmentCalibrationBuildSpec:
    return AssessmentCalibrationBuildSpec(
        analysis_scope="crypto-portfolio",
        analysis_behavior_hash="b" * 64,
        outcome_evaluation_version="assessment-outcome-v1",
        asset="BTC",
        symbol="BTCUSDT",
        horizon_minutes=240,
        direction=DirectionalView.UP,
        already_priced=PricedState.PARTIAL,
        uncertainty=AssessmentUncertainty.MEDIUM,
        training_start=NOW - timedelta(days=2),
        training_end=NOW - timedelta(hours=2),
        published_at=NOW,
        expected_edge_half_life_seconds=7_200,
    )


def test_assessment_calibration_is_computed_only_from_non_overlapping_outcomes() -> None:
    outcomes = tuple(
        _settled_assessment_outcome(
            index,
            signal_at=NOW - timedelta(hours=20 - index * 6),
            directional_return_bps=Decimal(str(10 + index * 10)),
        )
        for index in range(3)
    )
    policy = AssessmentForecastPolicy(
        version="assessment-forecast-v1",
        maximum_age_seconds=3_600,
        maximum_reference_market_age_seconds=30,
        minimum_sample_size=3,
        minimum_non_overlapping_sample_size=3,
    )

    calibration = AssessmentCalibrationBuilder(policy).build(
        outcomes,
        _calibration_spec(),
    )

    assert calibration.expected_gross_bps == Decimal("20")
    assert calibration.dispersion_bps == Decimal("10")
    assert calibration.conservative_gross_bps < Decimal("20")
    assert calibration.sample_size == 3
    assert calibration.non_overlapping_sample_size == 3
    assert calibration.analysis_behavior_hash == "b" * 64
    assert calibration.source_refs == tuple(sorted(item.outcome_id for item in outcomes))


def test_overlapping_assessment_outcomes_cannot_satisfy_calibration_gate() -> None:
    outcomes = tuple(
        _settled_assessment_outcome(
            index,
            signal_at=NOW - timedelta(hours=10 - index),
            directional_return_bps=Decimal("10"),
        )
        for index in range(3)
    )
    policy = AssessmentForecastPolicy(
        version="assessment-forecast-v1",
        maximum_age_seconds=3_600,
        maximum_reference_market_age_seconds=30,
        minimum_sample_size=3,
        minimum_non_overlapping_sample_size=3,
    )

    with pytest.raises(ValueError, match="非重叠"):
        AssessmentCalibrationBuilder(policy).build(
            outcomes,
            _calibration_spec(),
        )


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
