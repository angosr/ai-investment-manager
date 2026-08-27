"""Forward evidence for deterministic product mapping residuals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from investment_manager.forecast.models import ExposureDirection
from investment_manager.forecast.product.models import (
    ProductPayoffOutcome,
    ProductPayoffProjection,
)
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.kernel.identity import content_hash

PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION = "product-payoff-residual-evidence-v2"


class ProductPayoffEvidenceStatus(StrEnum):
    NO_SETTLED_SAMPLES = "NO_SETTLED_SAMPLES"
    COLLECTING = "COLLECTING"
    SUFFICIENT = "SUFFICIENT"


@dataclass(frozen=True, slots=True, order=True)
class ProductPayoffMappingIdentity:
    """One configured economic exposure and its complete product mapping."""

    economic_exposure_id: str
    projection_version: str
    instrument_keys: tuple[str, ...]
    maximum_rule_age_seconds: int

    def __post_init__(self) -> None:
        if not self.economic_exposure_id or not self.projection_version:
            raise ValueError("Product payoff mapping identity 不得为空")
        if (
            not self.instrument_keys
            or tuple(sorted(set(self.instrument_keys))) != self.instrument_keys
        ):
            raise ValueError("Product payoff mapping instruments 必须唯一且排序")
        if self.maximum_rule_age_seconds < 1:
            raise ValueError("Product payoff mapping rule age 必须为正数")

    @property
    def cohort_id(self) -> str:
        return content_hash(
            {
                "economic_exposure_id": self.economic_exposure_id,
                "projection_version": self.projection_version,
                "instrument_keys": self.instrument_keys,
                "maximum_rule_age_seconds": self.maximum_rule_age_seconds,
            }
        )

    def contains(self, projection: ProductPayoffProjection) -> bool:
        return (
            projection.economic_exposure_id == self.economic_exposure_id
            and projection.projection_version == self.projection_version
            and projection.target.legs[0].instrument.key in self.instrument_keys
        )


@dataclass(frozen=True, slots=True)
class ProductPayoffEvidence:
    evaluation_version: str
    mapping_cohort: tuple[ProductPayoffMappingIdentity, ...]
    status: ProductPayoffEvidenceStatus
    terminal_product_count: int
    settled_product_count: int
    unavailable_product_count: int
    source_forecast_count: int
    independent_source_forecast_count: int
    required_independent_source_forecasts: int
    mean_absolute_mapping_error_bps: Decimal | None
    mapping_conservative_coverage: Decimal | None
    mapping_residual_sign_accuracy: Decimal | None


@dataclass(frozen=True, slots=True)
class ProductPayoffEvaluationCase:
    source_forecast: BaseForecast
    source_outcome: ForecastOutcome
    projection: ProductPayoffProjection
    product_outcome: ProductPayoffOutcome


def evaluate_product_payoff_evidence(
    cases: tuple[ProductPayoffEvaluationCase, ...],
    *,
    mapping_cohort: tuple[ProductPayoffMappingIdentity, ...],
    product_outcome_version: str,
    forecast_outcome_version: str,
    required_independent_source_forecasts: int,
) -> ProductPayoffEvidence:
    """Evaluate only product residuals after removing the realized economic return."""

    if required_independent_source_forecasts < 1:
        raise ValueError("Product payoff 最小独立样本数必须为正数")
    if not mapping_cohort or tuple(sorted(set(mapping_cohort))) != mapping_cohort:
        raise ValueError("Product payoff mapping cohort 必须唯一且排序")
    for case in cases:
        projection = case.projection
        outcome = case.product_outcome
        if (
            case.source_forecast.forecast_id != projection.source_forecast_id
            or outcome.projection_id != projection.projection_id
            or outcome.source_forecast_id != projection.source_forecast_id
            or outcome.evaluation_version != product_outcome_version
            or case.source_outcome.decision_slot_id
            != case.source_forecast.decision_slot_id
            or case.source_outcome.contract_id != case.source_forecast.contract_id
            or case.source_outcome.evaluation_at != projection.evaluation_at
            or case.source_outcome.evaluation_version
            != forecast_outcome_version
            or not any(
                item.cohort_id == projection.mapping_cohort_id
                and item.contains(projection)
                for item in mapping_cohort
            )
        ):
            raise ValueError("Product payoff 评价输入身份不一致")
    settled = tuple(
        case
        for case in cases
        if case.product_outcome.status == ForecastOutcomeStatus.SETTLED
        and case.source_outcome.status == ForecastOutcomeStatus.SETTLED
    )
    unavailable = len(cases) - len(settled)
    by_decision: defaultdict[
        tuple[str, datetime],
        list[ProductPayoffEvaluationCase],
    ] = defaultdict(list)
    for case in cases:
        by_decision[(case.projection.source_forecast_id, case.projection.projected_at)].append(
            case
        )
    first_decision_by_source = {}
    for (source_forecast_id, projected_at), group in by_decision.items():
        current = first_decision_by_source.get(source_forecast_id)
        if current is None or projected_at < current[0]:
            first_decision_by_source[source_forecast_id] = (projected_at, group)
    first_source_groups = tuple(
        sorted(
            (item[1] for item in first_decision_by_source.values()),
            key=lambda group: (
                group[0].projection.evaluation_at,
                group[0].projection.projected_at,
                group[0].projection.source_forecast_id,
            ),
        )
    )
    source_groups = tuple(
        group
        for group in first_source_groups
        if all(case in settled for case in group)
    )
    independent = []
    previous_evaluation_at = None
    for group in source_groups:
        projected_at = min(item.projection.projected_at for item in group)
        if previous_evaluation_at is not None and projected_at < previous_evaluation_at:
            continue
        independent.append(group)
        previous_evaluation_at = group[0].projection.evaluation_at

    independent_cases = tuple(case for group in independent for case in group)
    independent_count = len(independent)
    if not independent_cases:
        return ProductPayoffEvidence(
            evaluation_version=PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION,
            mapping_cohort=mapping_cohort,
            status=ProductPayoffEvidenceStatus.NO_SETTLED_SAMPLES,
            terminal_product_count=len(cases),
            settled_product_count=len(settled),
            unavailable_product_count=unavailable,
            source_forecast_count=len(first_source_groups),
            independent_source_forecast_count=0,
            required_independent_source_forecasts=(
                required_independent_source_forecasts
            ),
            mean_absolute_mapping_error_bps=None,
            mapping_conservative_coverage=None,
            mapping_residual_sign_accuracy=None,
        )
    residuals = tuple(_mapping_residuals(case) for case in independent_cases)
    prediction_errors = tuple(
        abs(realized - expected) for expected, _conservative, realized in residuals
    )
    conservative_hits = tuple(
        realized >= conservative for _expected, conservative, realized in residuals
    )
    sign_hits = tuple(
        _sign(realized) == _sign(expected) for expected, _conservative, realized in residuals
    )

    return ProductPayoffEvidence(
        evaluation_version=PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION,
        mapping_cohort=mapping_cohort,
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
        mean_absolute_mapping_error_bps=_mean(prediction_errors),
        mapping_conservative_coverage=_fraction(conservative_hits),
        mapping_residual_sign_accuracy=_fraction(sign_hits),
    )


def _mapping_residuals(case: ProductPayoffEvaluationCase) -> tuple[Decimal, Decimal, Decimal]:
    source_outcome = case.source_outcome
    assert source_outcome.gross_target_return_bps is not None
    assert case.product_outcome.realized_gross_bps is not None
    direction = case.projection.target.legs[0].direction
    sign = Decimal("1") if direction == ExposureDirection.LONG else Decimal("-1")
    expected_reference = sign * case.source_forecast.expected_gross_bps
    realized_reference = sign * source_outcome.gross_target_return_bps
    return (
        case.projection.expected_gross_bps - expected_reference,
        case.projection.conservative_gross_bps - expected_reference,
        case.product_outcome.realized_gross_bps - realized_reference,
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
    "PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION",
    "ProductPayoffEvaluationCase",
    "ProductPayoffEvidence",
    "ProductPayoffEvidenceStatus",
    "ProductPayoffMappingIdentity",
    "evaluate_product_payoff_evidence",
]
