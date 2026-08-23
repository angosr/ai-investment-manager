"""Deterministic settlement of WorldModel mechanism tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from investment_manager.forecast.models import (
    ContextAssessment,
    ContextAssessmentSchemaVersion,
    ContextMechanismObservation,
    ContextPredicateOperator,
    ContextVerificationMatch,
    ContextVerificationPredicate,
    ContextVerificationResolution,
    ContextVerificationTest,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.state.decision.packet import (
    DecisionPacket,
    continuous_fact_numeric_values,
)

WORLD_MODEL_VERIFICATION_POLICY_VERSION = "mechanism-lineage-v2"


def observe_world_model(
    assessment: ContextAssessment,
    packet: DecisionPacket,
    *,
    previous: tuple[ContextMechanismObservation, ...] = (),
) -> tuple[ContextMechanismObservation, ...]:
    """Evaluate every due test against a later point-in-time packet.

    Missing features remain missing facts: no observation is fabricated.  A
    persistence requirement is satisfied only by consecutive matching packets.
    """

    if assessment.schema_version != ContextAssessmentSchemaVersion.WORLD_MODEL_V2:
        return ()
    if packet.as_of <= assessment.available_at:
        return ()
    previous_by_test: dict[str, ContextMechanismObservation] = {}
    previous_by_mechanism_contract: dict[tuple[str, str], ContextMechanismObservation] = {}
    for item in sorted(previous, key=lambda value: (value.observed_at, value.observation_id)):
        if (
            item.verification_policy_version == WORLD_MODEL_VERIFICATION_POLICY_VERSION
            and item.assessment_id == assessment.assessment_id
        ):
            previous_by_test[item.test_id] = item
        if (
            item.verification_policy_version == WORLD_MODEL_VERIFICATION_POLICY_VERSION
            and item.test_contract_hash is not None
        ):
            previous_by_mechanism_contract[(item.mechanism_id, item.test_contract_hash)] = item
    values = packet_feature_values(packet)
    observations: list[ContextMechanismObservation] = []
    for mechanism in assessment.mechanisms:
        for index, test in enumerate(mechanism.verification_tests):
            if packet.as_of > assessment.available_at + timedelta(
                minutes=test.evaluation_window_minutes
            ):
                continue
            value = values.get(test.feature_selector)
            if value is None:
                continue
            test_id = verification_test_id(
                assessment_id=assessment.assessment_id,
                mechanism_id=mechanism.mechanism_id,
                test_index=index,
                test=test,
            )
            test_contract_hash = verification_test_contract_hash(test)
            match = predicate_match(
                value,
                supports=test.supports_predicate,
                contradicts=test.contradicts_predicate,
            )
            prior = previous_by_test.get(test_id)
            if prior is None and mechanism.continuity_ref is not None:
                prior = previous_by_mechanism_contract.get(
                    (mechanism.continuity_ref, test_contract_hash)
                )
            support_streak = (
                (prior.support_streak if prior is not None else 0) + 1
                if match == ContextVerificationMatch.SUPPORTS
                else 0
            )
            contradiction_streak = (
                (prior.contradiction_streak if prior is not None else 0) + 1
                if match == ContextVerificationMatch.CONTRADICTS
                else 0
            )
            if match == ContextVerificationMatch.AMBIGUOUS:
                resolution = ContextVerificationResolution.AMBIGUOUS
            elif support_streak >= test.supports_predicate.persistence_observations:
                resolution = ContextVerificationResolution.SUPPORTED
            elif contradiction_streak >= test.contradicts_predicate.persistence_observations:
                resolution = ContextVerificationResolution.CONTRADICTED
            else:
                resolution = ContextVerificationResolution.PENDING
            observation_id = stable_id(
                "world_mechanism_observation",
                assessment.assessment_id,
                mechanism.mechanism_id,
                test_id,
                packet.packet_id,
                packet.as_of,
                str(value),
                match,
                support_streak,
                contradiction_streak,
                resolution,
                test_contract_hash,
                WORLD_MODEL_VERIFICATION_POLICY_VERSION,
            )
            observation = ContextMechanismObservation(
                observation_id=observation_id,
                assessment_id=assessment.assessment_id,
                mechanism_id=mechanism.mechanism_id,
                test_id=test_id,
                test_contract_hash=test_contract_hash,
                verification_policy_version=WORLD_MODEL_VERIFICATION_POLICY_VERSION,
                packet_id=packet.packet_id,
                feature_selector=test.feature_selector,
                observed_at=packet.as_of,
                value=value,
                match=match,
                support_streak=support_streak,
                contradiction_streak=contradiction_streak,
                resolution=resolution,
            )
            observations.append(observation)
            previous_by_test[test_id] = observation
            previous_by_mechanism_contract[(mechanism.mechanism_id, test_contract_hash)] = (
                observation
            )
    return tuple(observations)


def verification_test_id(
    *,
    assessment_id: str,
    mechanism_id: str,
    test_index: int,
    test: ContextVerificationTest,
) -> str:
    return stable_id(
        "world_mechanism_test",
        assessment_id,
        mechanism_id,
        test_index,
        test,
    )


def verification_test_contract_hash(test: ContextVerificationTest) -> str:
    """Stable verification identity that survives an explicit mechanism continuation."""

    return content_hash(test)


def predicate_match(
    value: Decimal,
    *,
    supports: ContextVerificationPredicate,
    contradicts: ContextVerificationPredicate,
) -> ContextVerificationMatch:
    support = _matches(value, supports)
    contradiction = _matches(value, contradicts)
    if support and contradiction:
        return ContextVerificationMatch.AMBIGUOUS
    if support:
        return ContextVerificationMatch.SUPPORTS
    if contradiction:
        return ContextVerificationMatch.CONTRADICTS
    return ContextVerificationMatch.NEITHER


def packet_feature_values(packet: DecisionPacket) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in packet.asset_states:
        for field in (
            "last",
            "return_fraction",
            "realized_volatility",
            "atr",
            "spread_bps",
            "volume_ratio",
        ):
            values[f"asset_state:{item.asset}.{field}"] = Decimal(str(getattr(item, field)))
    for item in packet.derivative_states:
        for field in (
            "mark_index_premium_bps",
            "executable_short_basis_bps",
            "perpetual_spread_bps",
            "last_funding_rate_bps",
            "trailing_funding_rate_mean_bps",
            "trailing_funding_rate_stddev_bps",
            "trailing_funding_positive_fraction",
            "spot_taker_buy_sell_ratio",
            "open_interest_change_fraction",
            "global_long_account_fraction",
            "taker_buy_sell_ratio",
        ):
            raw = getattr(item, field)
            if raw is not None:
                values[f"derivative_state:{item.asset}.{field}"] = Decimal(str(raw))
    for item in getattr(packet, "facts", ()):
        for field, value in continuous_fact_numeric_values(item).items():
            values[f"fact_state:{item.fact_type}.{field}"] = value
    return values


def _matches(value: Decimal, predicate: ContextVerificationPredicate) -> bool:
    if predicate.operator == ContextPredicateOperator.GT:
        return value > predicate.value
    if predicate.operator == ContextPredicateOperator.GTE:
        return value >= predicate.value
    if predicate.operator == ContextPredicateOperator.LT:
        return value < predicate.value
    if predicate.operator == ContextPredicateOperator.LTE:
        return value <= predicate.value
    if predicate.operator == ContextPredicateOperator.BETWEEN:
        assert predicate.upper_value is not None
        return predicate.value <= value <= predicate.upper_value
    raise ValueError(f"不支持的世界机制谓词: {predicate.operator}")
