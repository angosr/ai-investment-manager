from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from investment_manager.forecast.quant.runtime import (
    load_quant_forecast_artifact,
    quant_cell_key,
    quant_features_from_bars,
    quant_panel_projection,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.market.models import MarketBar


def _artifact_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "evidence"
        / "quant-forecasts"
        / "quant_forecast_artifact_b41990aeb6a3135ae636.json"
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


def test_frozen_quant_artifact_has_chronological_out_of_sample_increment() -> None:
    artifact = load_quant_forecast_artifact(
        _artifact_path(),
        expected_artifact_id="quant_forecast_artifact_b41990aeb6a3135ae636",
    )

    selected = next(
        item
        for item in artifact.candidate_evaluations
        if item.model_name == artifact.selected_model
    )
    assert selected.validation_brier < artifact.validation_unconditional_brier
    assert artifact.selected_blind_brier < artifact.blind_unconditional_brier
    assert artifact.development_sample_count == 10_507
    assert artifact.validation_sample_count == 3_502
    assert artifact.blind_sample_count == 3_503
    assert tuple(item.model_name for item in artifact.candidate_evaluations) == (
        "momentum",
        "momentum_volatility",
        "momentum_reversal_volatility",
    )
    assert all(item.cells for item in artifact.candidate_evaluations)


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
        decision_slot_id="test-slot",
    )
    assert panel["decision_slot_id"] == "test-slot"
    assert len(panel["candidate_predictions"]) == 3
    assert panel["maximum_bucket_probability_range"] > 0

    with pytest.raises(PointInTimeInputUnavailable, match="时间缺口"):
        quant_features_from_bars(_bars(gap_at=20))


def test_quant_artifact_rejects_content_under_an_existing_identity(tmp_path: Path) -> None:
    payload = json.loads(_artifact_path().read_text(encoding="utf-8"))
    payload["smoothing_strength"] = "21"
    path = tmp_path / "modified.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact_id"):
        load_quant_forecast_artifact(path)
