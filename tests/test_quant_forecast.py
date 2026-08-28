from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from investment_manager.forecast.context.producer import context_forecast_contract
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.quant.runtime import (
    QuantForecastArtifact,
    load_quant_forecast_artifact,
    quant_cell_key,
    quant_features_from_bars,
    quant_panel_projection,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.market.models import MarketBar
from investment_manager.settings import load_config


def _artifact_path(
    artifact_id: str = "quant_forecast_artifact_70430e567349f038e5b4",
) -> Path:
    return (
        Path(__file__).resolve().parents[1] / "evidence" / "quant-forecasts" / f"{artifact_id}.json"
    )


def _contract_for_artifact(artifact: QuantForecastArtifact) -> ForecastContract:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")
    policy = config.capital.context_forecast
    assert policy is not None
    target = next(
        item for item in policy.targets if item.outcome_family_id == artifact.outcome_family_id
    )
    instruments = {
        **{item.instrument.key: item.instrument for item in config.capital.execution_specs},
        **{item.key: item for item in config.capital.forecast_reference_instruments},
    }
    return context_forecast_contract(
        policy=policy,
        target_policy=target,
        instrument=instruments[target.reference_instrument_key],
        cost_semantics_version=config.capital.decision.cost_model_version,
    )


def _bars(*, gap_at: int | None = None) -> tuple[MarketBar, ...]:
    start = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    bars = []
    for index in range(49):
        offset = index + (1 if gap_at is not None and index >= gap_at else 0)
        at = start + timedelta(minutes=5 * offset)
        price = Decimal("100") + Decimal(index) / Decimal("10")
        bars.append(
            MarketBar(
                event_time=at,
                observed_at=at,
                open=price,
                high=price + Decimal("0.1"),
                low=price - Decimal("0.1"),
                close=price,
                volume=Decimal("10"),
            )
        )
    return tuple(bars)


@pytest.mark.parametrize(
    ("artifact_id", "selected_model", "validation_ranked", "blind_ranked"),
    (
        (
            "quant_forecast_artifact_70430e567349f038e5b4",
            "momentum_volatility",
            Decimal("0.1660342971934536799597448882"),
            Decimal("0.1451492180620373837441487391"),
        ),
        (
            "quant_forecast_artifact_c730529eb5cd931235d8",
            "momentum_volatility",
            Decimal("0.1853700710590541054562921038"),
            Decimal("0.1757957198816177466228240011"),
        ),
    ),
)
def test_frozen_quant_artifact_has_chronological_out_of_sample_increment(
    artifact_id: str,
    selected_model: str,
    validation_ranked: Decimal,
    blind_ranked: Decimal,
) -> None:
    artifact = load_quant_forecast_artifact(
        _artifact_path(artifact_id),
        expected_artifact_id=artifact_id,
    )

    selected = next(
        item
        for item in artifact.candidate_evaluations
        if item.model_name == artifact.selected_model
    )
    assert (
        selected.validation_ranked_probability_score
        < artifact.validation_unconditional_ranked_probability_score
    )
    assert (
        artifact.selected_blind_ranked_probability_score
        < artifact.blind_unconditional_ranked_probability_score
    )
    assert selected.validation_ranked_probability_score == validation_ranked
    assert artifact.selected_blind_ranked_probability_score == blind_ranked
    assert artifact.selected_model == selected_model
    assert selected.validation_worst_phase_ranked_probability_score == max(
        selected.validation_phase_ranked_probability_scores
    )
    strict_best = min(
        item.validation_worst_phase_ranked_probability_score
        for item in artifact.candidate_evaluations
    )
    assert selected.validation_worst_phase_ranked_probability_score <= (
        strict_best + artifact.selection_standard_error
    )
    assert selected == next(
        item
        for item in artifact.candidate_evaluations
        if all(
            model < baseline
            for model, baseline in zip(
                item.validation_phase_ranked_probability_scores,
                artifact.validation_unconditional_phase_ranked_probability_scores,
                strict=True,
            )
        )
        and item.validation_worst_phase_ranked_probability_score
        <= strict_best + artifact.selection_standard_error
    )
    assert artifact.selection_standard_error > 0
    assert artifact.development_sample_count == 10_504
    assert artifact.validation_sample_count == 3_499
    assert artifact.blind_sample_count == 3_503
    assert sum(artifact.validation_phase_sample_counts) == 3_499
    assert sum(artifact.blind_phase_sample_counts) == 3_503
    assert (
        max(artifact.validation_phase_sample_counts) - min(artifact.validation_phase_sample_counts)
        <= 1
    )
    assert max(artifact.blind_phase_sample_counts) - min(artifact.blind_phase_sample_counts) <= 1
    assert all(
        selected_score < baseline_score
        for selected_score, baseline_score in zip(
            artifact.selected_blind_phase_ranked_probability_scores,
            artifact.blind_unconditional_phase_ranked_probability_scores,
            strict=True,
        )
    )
    assert tuple(item.model_name for item in artifact.candidate_evaluations) == (
        "momentum",
        "momentum_volatility",
        "momentum_reversal_volatility",
    )
    assert all(item.cells for item in artifact.candidate_evaluations)
    assert selected.validation_mean_absolute_return_error_bps > 0
    assert Decimal("-1") <= selected.validation_return_correlation <= Decimal("1")
    assert artifact.selected_blind_mean_absolute_return_error_bps > 0
    assert Decimal("-1") <= artifact.selected_blind_return_correlation <= Decimal("1")
    feasibility = artifact.historical_capital_feasibility
    assert feasibility.status == "UNAVAILABLE"
    assert feasibility.reason_code == "POINT_IN_TIME_EXECUTION_FACTS_UNAVAILABLE"
    assert feasibility.checked_dataset_ids == (artifact.dataset_id,)
    assert "EXECUTABLE_BID_ASK_DEPTH_HISTORY" in feasibility.missing_fact_types
    assert "TIME_VERSIONED_EXECUTION_RULES" in feasibility.missing_fact_types
    assert "VERIFIED_FUNDING_SETTLEMENT_HISTORY" in feasibility.missing_fact_types


def test_quant_features_and_cell_are_deterministic_and_point_in_time() -> None:
    artifact = load_quant_forecast_artifact(_artifact_path())
    features = quant_features_from_bars(_bars())
    cell_key = quant_cell_key(
        artifact.selected_model,
        features,
        artifact.feature_thresholds,
    )

    assert features.return_60m_bps > 0
    assert features.return_240m_bps > features.return_60m_bps
    assert cell_key.startswith("momentum=HIGH|")
    probabilities = artifact.probabilities_for(features)
    assert sum((item.probability for item in probabilities), Decimal("0")) == 1
    candidate_probabilities = tuple(
        artifact.probabilities_for(features, model_name=item.model_name)
        for item in artifact.candidate_evaluations
    )
    assert len(set(candidate_probabilities)) > 1
    panel = quant_panel_projection(
        artifact,
        features,
        contract=_contract_for_artifact(artifact),
        decision_slot_id="test-slot",
    )
    assert panel["decision_slot_id"] == "test-slot"
    assert len(panel["candidate_predictions"]) == 3
    assert panel["maximum_bucket_probability_range"] > 0
    assert panel["panel_version"] == "quant-reliability-panel-v5"
    assert panel["quant_prior"]["cell_sample_count"] > 0
    assert panel["quant_prior"]["reliability"]["blind_ranked_skill"] > 0
    assert panel["quant_prior"]["reliability"]["blind_mean_absolute_return_error_bps"] > 0
    assert "blind_return_correlation" in panel["quant_prior"]["reliability"]

    with pytest.raises(PointInTimeInputUnavailable, match="时间缺口"):
        quant_features_from_bars(_bars(gap_at=20))


def test_quant_artifact_rejects_content_under_an_existing_identity(tmp_path: Path) -> None:
    payload = json.loads(_artifact_path().read_text(encoding="utf-8"))
    payload["smoothing_strength"] = "21"
    path = tmp_path / "modified.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact_id"):
        load_quant_forecast_artifact(path)
