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

PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION = "product-payoff-residual-evidence-v4"


class ProductPayoffEvidenceStatus(StrEnum):
    NO_SETTLED_SAMPLES = "NO_SETTLED_SAMPLES"
    OBSERVED = "OBSERVED"


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
    non_overlapping_panel_count: int
    mean_absolute_mapping_error_bps: Decimal | None
    mapping_conservative_coverage: Decimal | None
    mapping_residual_sign_accuracy: Decimal | None


@dataclass(frozen=True, slots=True)
class ProductPayoffEvaluationCase:
    source_forecast: BaseForecast
    source_outcome: ForecastOutcome
    projection: ProductPayoffProjection
    product_outcome: ProductPayoffOutcome


@dataclass(frozen=True, slots=True)
class _ProductPayoffPanel:
    producer_behavior_id: str
    information_cutoff_at: datetime
    projected_at: datetime
    evaluation_at: datetime
    cases: tuple[ProductPayoffEvaluationCase, ...]


def evaluate_product_payoff_evidence(
    cases: tuple[ProductPayoffEvaluationCase, ...],
    *,
    mapping_cohort: tuple[ProductPayoffMappingIdentity, ...],
    product_outcome_version: str,
    forecast_outcome_version: str,
) -> ProductPayoffEvidence:
    """Evaluate only product residuals after removing the realized economic return."""

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
    panel_groups: defaultdict[
        tuple[str, datetime, datetime],
        list[ProductPayoffEvaluationCase],
    ] = defaultdict(list)
    for group in first_source_groups:
        source = group[0].source_forecast
        panel_groups[
            (
                source.producer_behavior_id,
                source.information_cutoff_at,
                group[0].projection.projected_at,
            )
        ].extend(group)
    settled_ids = {
        case.product_outcome.outcome_id
        for case in settled
    }
    complete_panels = tuple(
        _ProductPayoffPanel(
            producer_behavior_id=producer_behavior_id,
            information_cutoff_at=information_cutoff_at,
            projected_at=projected_at,
            evaluation_at=max(item.projection.evaluation_at for item in group),
            cases=tuple(
                sorted(
                    group,
                    key=lambda item: (
                        item.source_forecast.outcome_family_id,
                        item.projection.projection_id,
                    ),
                )
            ),
        )
        for (
            producer_behavior_id,
            information_cutoff_at,
            projected_at,
        ), group in panel_groups.items()
        if all(item.product_outcome.outcome_id in settled_ids for item in group)
    )
    independent = []
    previous_evaluation_at = None
    for panel in sorted(
        complete_panels,
        key=lambda item: (
            item.projected_at,
            item.evaluation_at,
            item.producer_behavior_id,
            item.information_cutoff_at,
        ),
    ):
        if (
            previous_evaluation_at is not None
            and panel.projected_at < previous_evaluation_at
        ):
            continue
        independent.append(panel)
        previous_evaluation_at = panel.evaluation_at

    independent_panels = tuple(independent)
    if not independent_panels:
        return ProductPayoffEvidence(
            evaluation_version=PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION,
            mapping_cohort=mapping_cohort,
            status=ProductPayoffEvidenceStatus.NO_SETTLED_SAMPLES,
            terminal_product_count=len(cases),
            settled_product_count=len(settled),
            unavailable_product_count=unavailable,
            source_forecast_count=len(first_source_groups),
            non_overlapping_panel_count=0,
            mean_absolute_mapping_error_bps=None,
            mapping_conservative_coverage=None,
            mapping_residual_sign_accuracy=None,
        )
    panel_residuals = tuple(
        tuple(_mapping_residuals(case) for case in panel.cases)
        for panel in independent_panels
    )
    prediction_errors = tuple(
        _required_mean(
            tuple(
                abs(realized - expected)
                for expected, _conservative, realized in residuals
            )
        )
        for residuals in panel_residuals
    )
    conservative_hits = tuple(
        _required_fraction(
            tuple(
                realized >= conservative
                for _expected, conservative, realized in residuals
            )
        )
        for residuals in panel_residuals
    )
    sign_hits = tuple(
        _required_fraction(
            tuple(
                _sign(realized) == _sign(expected)
                for expected, _conservative, realized in residuals
            )
        )
        for residuals in panel_residuals
    )

    return ProductPayoffEvidence(
        evaluation_version=PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION,
        mapping_cohort=mapping_cohort,
        status=ProductPayoffEvidenceStatus.OBSERVED,
        terminal_product_count=len(cases),
        settled_product_count=len(settled),
        unavailable_product_count=unavailable,
        source_forecast_count=len(first_source_groups),
        non_overlapping_panel_count=len(independent_panels),
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


def _required_mean(values: tuple[Decimal, ...]) -> Decimal:
    result = _mean(values)
    assert result is not None
    return result


def _required_fraction(values: tuple[bool, ...]) -> Decimal:
    result = _fraction(values)
    assert result is not None
    return result


__all__ = [
    "PRODUCT_PAYOFF_EVIDENCE_EVALUATION_VERSION",
    "ProductPayoffEvaluationCase",
    "ProductPayoffEvidence",
    "ProductPayoffEvidenceStatus",
    "ProductPayoffMappingIdentity",
    "evaluate_product_payoff_evidence",
]
