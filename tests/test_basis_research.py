from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from investment_manager.research.basis import (
    BasisMappingEvaluationStatus,
    evaluate_basis_mapping,
)


def _plan(*, start: datetime, end: datetime) -> dict:
    return {
        "schema_version": "product-mapping-candidate-plan-v1",
        "plan_id": "paxg-perpetual-basis-4h-v1",
        "evaluation_family_id": "paxg-product-mapping-4h",
        "data": {
            "symbol": "PAXGUSDT",
            "interval": "1h",
            "source_window": [start, end],
        },
        "target": {"horizon_minutes": 240, "decision_cadence_minutes": 60},
        "comparison": {
            "baseline": {"name": "basis_persistence"},
            "candidate": {"name": "full_basis_convergence"},
            "fitted_parameters": "none",
        },
        "evaluation": {
            "chronological_split": [
                "development_60pct",
                "validation_20pct",
                "blind_20pct",
            ],
            "primary_metric": "exit_basis_mae_bps",
        },
    }


def _datasets(
    basis_bps: tuple[Decimal, ...],
    *,
    invalid_blind_prices: bool = False,
):
    start = datetime(2025, 4, 1, tzinfo=UTC)
    end = start + timedelta(hours=len(basis_bps))
    open_times = tuple(start + timedelta(hours=index) for index in range(len(basis_bps)))
    spot_bars = tuple(
        SimpleNamespace(
            open_time=at,
            close=(
                Decimal("0")
                if invalid_blind_prices and index >= int(len(basis_bps) * 0.8)
                else Decimal("100")
            ),
        )
        for index, at in enumerate(open_times)
    )
    carry_bars = tuple(
        SimpleNamespace(
            open_time=at,
            contract_close=Decimal("100") * (Decimal("1") + basis / Decimal("10000")),
        )
        for at, basis in zip(open_times, basis_bps, strict=True)
    )
    common = {
        "symbol": "PAXGUSDT",
        "interval": "1h",
        "requested_start": start,
        "requested_end": end,
        "collected_at": end + timedelta(hours=1),
    }
    spot = SimpleNamespace(
        manifest=SimpleNamespace(dataset_id="spot-v1", **common),
        bars=spot_bars,
    )
    carry = SimpleNamespace(
        manifest=SimpleNamespace(
            dataset_id="carry-v1",
            spot_dataset_id="spot-v1",
            funding_dataset_id=None,
            **common,
        ),
        bars=carry_bars,
        settlements=(),
    )
    return _plan(start=start, end=end), spot, carry


def _evaluate(plan, spot, carry):
    return evaluate_basis_mapping(
        plan=plan,
        plan_registration_commit="registration-commit",
        plan_registered_at=datetime(2026, 8, 28, tzinfo=UTC),
        evaluator_code_version="evaluator-commit",
        spot=spot,
        carry=carry,
    )


def test_basis_validation_rejection_does_not_evaluate_blind_prices() -> None:
    plan, spot, carry = _datasets(
        tuple(Decimal("100") for _ in range(120)),
        invalid_blind_prices=True,
    )

    artifact = _evaluate(plan, spot, carry)

    assert artifact.status == BasisMappingEvaluationStatus.REJECTED
    assert artifact.validation.candidate_wins_every_phase is False
    assert artifact.blind_evaluated is False
    assert artifact.blind is None


def test_basis_convergence_must_win_validation_and_blind_in_every_phase() -> None:
    basis = tuple(Decimal("100") * (Decimal("0.8") ** index) for index in range(120))
    plan, spot, carry = _datasets(basis)

    artifact = _evaluate(plan, spot, carry)

    assert artifact.status == BasisMappingEvaluationStatus.FORWARD_RESEARCH
    assert artifact.validation.candidate_wins_every_phase is True
    assert artifact.blind_evaluated is True
    assert artifact.blind is not None
    assert artifact.blind.candidate_wins_every_phase is True
    assert artifact.capital_feasibility == "UNAVAILABLE"
