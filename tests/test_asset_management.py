from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_manager.forecast.models import (
    AssessmentUncertainty,
    BaseForecast,
    CalibratedForecast,
    ContextAssessment,
    ContextView,
    DirectionalView,
    ForecastRole,
    PricedState,
)
from investment_manager.information.models import (
    SourceObservation,
    SourceTier,
)
from investment_manager.portfolio.models import (
    AssetTarget,
    PortfolioTarget,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    DeltaCategory,
    FactRevisionStatus,
    MaterialDelta,
    Materiality,
    StateSnapshot,
)

NOW = datetime(2026, 8, 20, 10, 30, tzinfo=UTC)
HASH = "a" * 64


def _source_observation() -> SourceObservation:
    return SourceObservation(
        observation_id="obs-1",
        source_id="cftc",
        source_tier=SourceTier.FIRST_PARTY,
        source_record_id="event-1",
        source_published_at=NOW - timedelta(minutes=1),
        observed_at=NOW,
        payload_hash=HASH,
        payload_ref="raw://obs-1",
    )


def _context_view(*, asset: str = "BTC") -> ContextView:
    return ContextView(
        asset=asset,
        horizon_minutes=240,
        direction=DirectionalView.UP,
        already_priced=PricedState.PARTIAL,
        uncertainty=AssessmentUncertainty.HIGH,
        evidence_ids=("fact-1",),
        invalidation_conditions=("official-retraction",),
    )


def _asset_target(*, symbol: str = "BTCUSDT") -> AssetTarget:
    return AssetTarget(
        symbol=symbol,
        desired_quote_notional=Decimal("1000"),
        forecast_ids=("forecast-1",),
        conservative_gross_bps=Decimal("20"),
        estimated_variable_cost_bps=Decimal("8"),
        conservative_net_bps=Decimal("12"),
        reason_codes=("NET_EDGE_POSITIVE",),
    )


def test_source_observation_rejects_future_claimed_publication() -> None:
    payload = _source_observation().model_dump()
    payload["source_published_at"] = NOW + timedelta(seconds=1)

    with pytest.raises(ValidationError, match="发布时间不能晚于"):
        SourceObservation.model_validate(payload)


def test_fact_revision_requires_sorted_unique_risk_identity() -> None:
    with pytest.raises(ValidationError, match="risk_factors 必须唯一且排序"):
        CanonicalFactRevision(
            fact_id="fact-1",
            revision_id="revision-1",
            projection_version="fact-projection-v1",
            fact_type="REGULATORY_EVENT",
            status=FactRevisionStatus.ACTIVE,
            event_time=NOW + timedelta(hours=1),
            observed_at=NOW,
            headline="CFTC meeting",
            claim="The meeting is scheduled.",
            affected_assets=("BTC", "ETH"),
            risk_factors=("REGULATION", "REGULATION"),
            source_observation_ids=("obs-1",),
            revision_hash=HASH,
        )


def test_state_snapshot_rejects_non_deterministic_reference_order() -> None:
    with pytest.raises(ValidationError, match="fact_revision_ids 必须唯一且排序"):
        StateSnapshot(
            state_id="state-1",
            projection_version="state-projection-v1",
            analysis_scope="crypto-risk",
            as_of=NOW,
            built_at=NOW,
            fact_revision_ids=("fact-2", "fact-1"),
            content_hash=HASH,
        )


def test_material_delta_requires_real_referenced_change() -> None:
    with pytest.raises(ValidationError, match="必须引用事实或特征变化"):
        MaterialDelta(
            delta_id="delta-1",
            policy_version="delta-policy-v1",
            analysis_scope="crypto-risk",
            previous_state_id="state-0",
            current_state_id="state-1",
            observed_at=NOW,
            expires_at=NOW + timedelta(minutes=30),
            category=DeltaCategory.FIRST_PARTY_FACT,
            materiality=Materiality.HIGH,
            affected_assets=("BTC", "ETH"),
            risk_factors=("REGULATION",),
            horizons_minutes=(60, 240),
            reason_codes=("OFFICIAL_REVISION",),
            content_hash=HASH,
        )


def test_context_assessment_is_multi_asset_but_unique_per_horizon() -> None:
    assessment = ContextAssessment(
        assessment_id="assessment-1",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        as_of=NOW,
        available_at=NOW + timedelta(seconds=20),
        analysis_behavior_hash=HASH,
        decision_packet_hash=HASH,
        trigger_ids=("delta-1",),
        market_mechanism="Regulatory clarity can alter the risk premium.",
        views=(_context_view(asset="BTC"), _context_view(asset="ETH")),
    )

    assert tuple(item.asset for item in assessment.views) == ("BTC", "ETH")

    payload = assessment.model_dump()
    payload["views"] = (payload["views"][1], payload["views"][0])
    with pytest.raises(ValidationError, match="资产/时域唯一且排序"):
        ContextAssessment.model_validate(payload)


def test_context_assessment_cannot_smuggle_an_order_field() -> None:
    payload = ContextAssessment(
        assessment_id="assessment-1",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        as_of=NOW,
        available_at=NOW + timedelta(seconds=20),
        analysis_behavior_hash=HASH,
        decision_packet_hash=HASH,
        trigger_ids=("delta-1",),
        market_mechanism="Regulatory clarity can alter the risk premium.",
        views=(_context_view(),),
    ).model_dump()
    payload["order_type"] = "MARKET"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ContextAssessment.model_validate(payload)


def test_base_forecast_rejects_free_analysis_latency() -> None:
    with pytest.raises(ValidationError, match="时间顺序非法"):
        BaseForecast(
            forecast_id="forecast-1",
            producer_id="trend",
            producer_version="v1",
            forecast_family="TREND",
            symbol="BTCUSDT",
            horizon_minutes=240,
            direction=DirectionalView.UP,
            observed_at=NOW,
            available_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(hours=4),
            raw_score=Decimal("1.2"),
            input_refs=("feature-1",),
        )


@pytest.mark.parametrize(
    ("role", "base_id", "assessment_id"),
    [
        (ForecastRole.PROGRAM_BASE, "base-1", None),
        (ForecastRole.AI_EVENT, None, "assessment-1"),
        (ForecastRole.AI_ADJUSTED, "base-1", "assessment-1"),
    ],
)
def test_calibrated_forecast_roles_have_one_unambiguous_provenance(
    role: ForecastRole,
    base_id: str | None,
    assessment_id: str | None,
) -> None:
    forecast = CalibratedForecast(
        forecast_id="calibrated-1",
        role=role,
        producer_id="calibration",
        producer_version="v1",
        forecast_family="EVENT",
        symbol="BTCUSDT",
        horizon_minutes=240,
        direction=DirectionalView.UP,
        reference_price=Decimal("100000"),
        expected_edge_half_life_seconds=3600,
        available_at=NOW,
        valid_until=NOW + timedelta(hours=1),
        base_forecast_id=base_id,
        assessment_id=assessment_id,
        expected_gross_bps=Decimal("25"),
        conservative_gross_bps=Decimal("15"),
        dispersion_bps=Decimal("30"),
        calibration_ref="calibration-v1",
        calibration_sample_size=40,
        non_overlapping_sample_size=30,
        input_refs=("input-1",),
    )

    assert forecast.role == role


def test_calibrated_forecast_rejects_role_reference_mismatch() -> None:
    with pytest.raises(ValidationError, match="base_forecast_id 不匹配"):
        CalibratedForecast(
            forecast_id="calibrated-1",
            role=ForecastRole.AI_EVENT,
            producer_id="calibration",
            producer_version="v1",
            forecast_family="EVENT",
            symbol="BTCUSDT",
            horizon_minutes=240,
            direction=DirectionalView.UP,
            reference_price=Decimal("100000"),
            expected_edge_half_life_seconds=3600,
            available_at=NOW,
            valid_until=NOW + timedelta(hours=1),
            base_forecast_id="base-1",
            assessment_id="assessment-1",
            expected_gross_bps=Decimal("25"),
            conservative_gross_bps=Decimal("15"),
            dispersion_bps=Decimal("30"),
            calibration_ref="calibration-v1",
            calibration_sample_size=40,
            non_overlapping_sample_size=30,
            input_refs=("input-1",),
        )


def test_portfolio_target_rejects_leverage_and_duplicate_assets() -> None:
    with pytest.raises(ValidationError, match="不能超过参考权益"):
        PortfolioTarget(
            target_id="target-1",
            cycle_id="cycle-1",
            portfolio_id="mock-main",
            policy_version="portfolio-v1",
            as_of=NOW,
            valid_until=NOW + timedelta(minutes=30),
            reference_equity=Decimal("10000"),
            targets=(
                _asset_target().model_copy(
                    update={"desired_quote_notional": Decimal("10001")}
                ),
            ),
        )

    with pytest.raises(ValidationError, match="资产必须唯一且排序"):
        PortfolioTarget(
            target_id="target-1",
            cycle_id="cycle-1",
            portfolio_id="mock-main",
            policy_version="portfolio-v1",
            as_of=NOW,
            valid_until=NOW + timedelta(minutes=30),
            reference_equity=Decimal("10000"),
            targets=(_asset_target(), _asset_target()),
        )


def test_asset_target_net_edge_has_one_formula() -> None:
    payload = _asset_target().model_dump()
    payload["conservative_net_bps"] = Decimal("13")

    with pytest.raises(ValidationError, match="净收益必须等于"):
        AssetTarget.model_validate(payload)


def test_portfolio_target_can_represent_all_cash() -> None:
    target = PortfolioTarget(
        target_id="target-cash",
        cycle_id="cycle-1",
        portfolio_id="mock-main",
        policy_version="portfolio-v1",
        as_of=NOW,
        valid_until=NOW + timedelta(minutes=30),
        reference_equity=Decimal("10000"),
    )

    assert target.targets == ()
