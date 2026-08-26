"""Forward evidence for deterministic product mapping and ranking."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from investment_manager.forecast.product.models import (
    ProductPayoffOutcome,
    ProductPayoffProjection,
)
from investment_manager.forecast.results import ForecastOutcomeStatus


class ProductPayoffEvidenceStatus(StrEnum):
    NO_SETTLED_SAMPLES = "NO_SETTLED_SAMPLES"
    COLLECTING = "COLLECTING"
    SUFFICIENT = "SUFFICIENT"


@dataclass(frozen=True, slots=True)
class ProductPayoffEvidence:
    evaluation_version: str
    status: ProductPayoffEvidenceStatus
    terminal_product_count: int
    settled_product_count: int
    unavailable_product_count: int
    source_forecast_count: int
    independent_source_forecast_count: int
    required_independent_source_forecasts: int
    comparable_source_forecast_count: int
    mean_absolute_prediction_error_bps: Decimal | None
    conservative_coverage: Decimal | None
    payoff_sign_accuracy: Decimal | None
    product_ranking_accuracy: Decimal | None
    mean_product_selection_regret_bps: Decimal | None


def evaluate_product_payoff_evidence(
    cases: tuple[tuple[ProductPayoffProjection, ProductPayoffOutcome], ...],
    *,
    evaluation_version: str,
    required_independent_source_forecasts: int,
) -> ProductPayoffEvidence:
    """Evaluate product mapping separately from the one economic Forecast score."""

    if required_independent_source_forecasts < 1:
        raise ValueError("Product payoff 最小独立样本数必须为正数")
    for projection, outcome in cases:
        if (
            outcome.projection_id != projection.projection_id
            or outcome.source_forecast_id != projection.source_forecast_id
            or outcome.evaluation_version != evaluation_version
        ):
            raise ValueError("Product payoff 评价输入身份不一致")
    settled = tuple(
        (projection, outcome)
        for projection, outcome in cases
        if outcome.status == ForecastOutcomeStatus.SETTLED
    )
    unavailable = len(cases) - len(settled)
    by_decision: defaultdict[
        tuple[str, datetime],
        list[tuple[ProductPayoffProjection, ProductPayoffOutcome]],
    ] = defaultdict(list)
    for case in cases:
        by_decision[(case[0].source_forecast_id, case[0].projected_at)].append(case)
    first_decision_by_source = {}
    for (source_forecast_id, projected_at), group in by_decision.items():
        current = first_decision_by_source.get(source_forecast_id)
        if current is None or projected_at < current[0]:
            first_decision_by_source[source_forecast_id] = (projected_at, group)
    first_source_groups = tuple(
        sorted(
            (item[1] for item in first_decision_by_source.values()),
            key=lambda group: (
                group[0][0].evaluation_at,
                group[0][0].projected_at,
                group[0][0].source_forecast_id,
            ),
        )
    )
    source_groups = tuple(
        group
        for group in first_source_groups
        if all(outcome.status == ForecastOutcomeStatus.SETTLED for _projection, outcome in group)
    )
    independent = []
    previous_evaluation_at = None
    for group in source_groups:
        projected_at = min(item[0].projected_at for item in group)
        if previous_evaluation_at is not None and projected_at < previous_evaluation_at:
            continue
        independent.append(group)
        previous_evaluation_at = group[0][0].evaluation_at

    independent_cases = tuple(case for group in independent for case in group)
    independent_count = len(independent)
    if not independent_cases:
        return ProductPayoffEvidence(
            evaluation_version=evaluation_version,
            status=ProductPayoffEvidenceStatus.NO_SETTLED_SAMPLES,
            terminal_product_count=len(cases),
            settled_product_count=len(settled),
            unavailable_product_count=unavailable,
            source_forecast_count=len(first_source_groups),
            independent_source_forecast_count=0,
            required_independent_source_forecasts=(
                required_independent_source_forecasts
            ),
            comparable_source_forecast_count=0,
            mean_absolute_prediction_error_bps=None,
            conservative_coverage=None,
            payoff_sign_accuracy=None,
            product_ranking_accuracy=None,
            mean_product_selection_regret_bps=None,
        )
    prediction_errors = tuple(
        abs(outcome.realized_gross_bps - projection.expected_gross_bps)
        for projection, outcome in independent_cases
        if outcome.realized_gross_bps is not None
    )
    conservative_hits = tuple(
        outcome.realized_gross_bps >= projection.conservative_gross_bps
        for projection, outcome in independent_cases
        if outcome.realized_gross_bps is not None
    )
    sign_hits = tuple(
        _sign(outcome.realized_gross_bps) == _sign(projection.expected_gross_bps)
        for projection, outcome in independent_cases
        if outcome.realized_gross_bps is not None
    )
    comparable = tuple(group for group in independent if len(group) > 1)
    ranking_hits = []
    regrets = []
    for group in comparable:
        predicted = max(
            group,
            key=lambda item: (
                item[0].conservative_gross_bps,
                item[0].projection_id,
            ),
        )
        realized = max(
            group,
            key=lambda item: (
                item[1].realized_gross_bps,
                item[0].projection_id,
            ),
        )
        ranking_hits.append(predicted[0].projection_id == realized[0].projection_id)
        assert predicted[1].realized_gross_bps is not None
        assert realized[1].realized_gross_bps is not None
        regrets.append(
            realized[1].realized_gross_bps - predicted[1].realized_gross_bps
        )

    return ProductPayoffEvidence(
        evaluation_version=evaluation_version,
        status=(
            ProductPayoffEvidenceStatus.SUFFICIENT
            if independent_count >= required_independent_source_forecasts
            else ProductPayoffEvidenceStatus.COLLECTING
        ),
        terminal_product_count=len(cases),
        settled_product_count=len(settled),
        unavailable_product_count=unavailable,
        source_forecast_count=len(first_source_groups),
        independent_source_forecast_count=independent_count,
        required_independent_source_forecasts=required_independent_source_forecasts,
        comparable_source_forecast_count=len(comparable),
        mean_absolute_prediction_error_bps=_mean(prediction_errors),
        conservative_coverage=_fraction(conservative_hits),
        payoff_sign_accuracy=_fraction(sign_hits),
        product_ranking_accuracy=_fraction(tuple(ranking_hits)),
        mean_product_selection_regret_bps=_mean(tuple(regrets)),
    )


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    return None if not values else sum(values, Decimal("0")) / len(values)


def _fraction(values: tuple[bool, ...]) -> Decimal | None:
    if not values:
        return None
    return Decimal(sum(values)) / Decimal(len(values))


__all__ = [
    "ProductPayoffEvidence",
    "ProductPayoffEvidenceStatus",
    "evaluate_product_payoff_evidence",
]
