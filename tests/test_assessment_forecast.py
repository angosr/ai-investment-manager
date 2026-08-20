from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quant_core.assessment_forecast import (
    AssessmentForecastPolicy,
    AssessmentForecastProjector,
    AssessmentViewCalibration,
    build_assessment_view_calibration,
)
from quant_core.asset_management import (
    AssessmentUncertainty,
    ContextAssessment,
    ContextView,
    ForecastRole,
    PricedState,
)
from quant_core.decision_packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDelta,
    PacketPortfolioState,
    RequiredView,
)
from quant_core.domain import DirectionalView

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
HASH = "a" * 64


def _packet() -> DecisionPacket:
    return DecisionPacket(
        packet_id="packet-1",
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
        content_hash=HASH,
    )


def _assessment() -> ContextAssessment:
    return ContextAssessment(
        assessment_id="assessment-1",
        analysis_scope="crypto-portfolio",
        mandate_version="mandate-v1",
        as_of=NOW,
        available_at=NOW + timedelta(seconds=20),
        analysis_behavior_hash="b" * 64,
        decision_packet_hash=HASH,
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
        "asset": "BTC",
        "symbol": "BTCUSDT",
        "horizon_minutes": 240,
        "direction": DirectionalView.UP,
        "already_priced": PricedState.PARTIAL,
        "uncertainty": AssessmentUncertainty.MEDIUM,
        "trained_through": NOW - timedelta(days=2),
        "available_at": NOW - timedelta(days=1),
        "expected_edge_half_life_seconds": 7_200,
        "expected_gross_bps": Decimal("18"),
        "conservative_gross_bps": Decimal("7"),
        "dispersion_bps": Decimal("24"),
        "sample_size": 60,
        "non_overlapping_sample_size": 35,
        "source_refs": ("paired-forward-report-v1",),
    }
    values.update(updates)
    return build_assessment_view_calibration(**values)


def _projector() -> AssessmentForecastProjector:
    return AssessmentForecastProjector(
        AssessmentForecastPolicy(
            version="assessment-forecast-v1",
            maximum_age_seconds=3_600,
            minimum_sample_size=40,
            minimum_non_overlapping_sample_size=30,
        )
    )


def test_exact_point_in_time_calibration_produces_ai_event_forecast() -> None:
    result = _projector().project(
        packet=_packet(),
        assessment=_assessment(),
        calibrations=(_calibration(),),
    )

    assert result.uncalibrated_views == ()
    assert len(result.forecasts) == 1
    forecast = result.forecasts[0]
    assert forecast.role == ForecastRole.AI_EVENT
    assert forecast.symbol == "BTCUSDT"
    assert forecast.reference_price == Decimal("70000")
    assert forecast.assessment_id == "assessment-1"
    assert forecast.base_forecast_id is None
    assert forecast.conservative_gross_bps == Decimal("7")
    assert forecast.valid_until == _assessment().available_at + timedelta(hours=1)


def test_missing_or_underpowered_calibration_cannot_grant_ai_capital_signal() -> None:
    missing = _projector().project(
        packet=_packet(),
        assessment=_assessment(),
        calibrations=(),
    )
    underpowered = _projector().project(
        packet=_packet(),
        assessment=_assessment(),
        calibrations=(_calibration(non_overlapping_sample_size=29),),
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
        )


def test_calibration_identity_is_content_addressed() -> None:
    calibration = _calibration()
    payload = calibration.model_dump()
    payload["expected_gross_bps"] = Decimal("99")

    with pytest.raises(ValueError, match="content_hash"):
        AssessmentViewCalibration.model_validate(payload)
