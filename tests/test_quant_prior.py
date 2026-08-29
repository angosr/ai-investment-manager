from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investment_manager.research.quant_prior import (
    _expert_value,
    _mixture,
    _rolling_baselines,
    _Sample,
    load_orthogonal_quant_prior_plan,
)


def _sample(
    *, cutoff_day: int, outcome_day: int, phase: int, return_bps: str
) -> _Sample:
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    return _Sample(
        cutoff_at=epoch + timedelta(days=cutoff_day),
        outcome_at=epoch + timedelta(days=outcome_day),
        phase=phase,
        return_bps=Decimal(return_bps),
        future_variance=Decimal("0.01"),
        trend=Decimal("0"),
        reversal=Decimal("0"),
        har_inputs=(Decimal("0.01"), Decimal("0.01"), Decimal("0.01")),
    )


def test_quant_prior_plan_is_one_frozen_shared_contract() -> None:
    plan = load_orthogonal_quant_prior_plan(
        Path("config/research/orthogonal-quant-prior-72h-v1.yaml")
    )

    assert plan.contract.evaluation_phases == (0, 1, 2)
    assert tuple(item.expert_id for item in plan.experts) == (
        "time_series_momentum",
        "standardized_reversal",
        "har_realized_volatility",
    )
    assert plan.scope.candidate_count == 1
    assert plan.scope.result_permission == "REJECTION_OR_FORWARD_RESEARCH_ONLY"


def test_rolling_baseline_uses_only_same_phase_settled_outcomes() -> None:
    visible = _sample(cutoff_day=0, outcome_day=3, phase=0, return_bps="-20")
    wrong_phase = _sample(cutoff_day=1, outcome_day=4, phase=1, return_bps="20")
    not_settled = _sample(cutoff_day=3, outcome_day=7, phase=0, return_bps="20")
    target = _sample(cutoff_day=6, outcome_day=9, phase=0, return_bps="0")

    result = _rolling_baselines(
        (visible, wrong_phase, not_settled, target),
        (target,),
        (Decimal("-10"), Decimal("10")),
        3,
    )

    assert result == ((Decimal("1"), Decimal("0"), Decimal("0")),)


def test_quant_mixture_preserves_probability_contract() -> None:
    result = _mixture(
        (
            (Decimal("0.2"), Decimal("0.3"), Decimal("0.5")),
            (Decimal("0.4"), Decimal("0.1"), Decimal("0.5")),
        )
    )

    assert result == (Decimal("0.3"), Decimal("0.2"), Decimal("0.5"))
    assert sum(result, Decimal("0")) == 1


def test_direction_experts_do_not_depend_on_har_coefficients() -> None:
    sample = _sample(cutoff_day=0, outcome_day=3, phase=0, return_bps="0")

    assert _expert_value(
        "time_series_momentum", sample, har_coefficients=()
    ) == Decimal("0")
    assert _expert_value(
        "standardized_reversal", sample, har_coefficients=()
    ) == Decimal("0")
