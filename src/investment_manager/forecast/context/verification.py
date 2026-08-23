"""Deterministic settlement of WorldModel mechanism tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
    PacketDerivativeState,
    continuous_fact_numeric_values,
)

WORLD_MODEL_VERIFICATION_POLICY_VERSION = "source-observation-v4"


@dataclass(frozen=True, slots=True)
class PacketFeatureObservation:
    value: Decimal
    source_ref: str
    source_observed_at: datetime


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
    features = packet_feature_observations(packet)
    observations: list[ContextMechanismObservation] = []
    for mechanism in assessment.mechanisms:
        baseline_evidence_ids = {
            evidence_id
            for node in mechanism.causal_chain
            for evidence_id in node.evidence_ids
        } | set(mechanism.conflicting_evidence_ids)
        for index, test in enumerate(mechanism.verification_tests):
            if packet.as_of > assessment.available_at + timedelta(
                minutes=test.evaluation_window_minutes
            ):
                continue
            feature = features.get(test.feature_selector)
            if feature is None:
                continue
            value = feature.value
            feature_observation_ref = feature.source_ref
            feature_observed_at = feature.source_observed_at
            if feature_observed_at <= assessment.available_at:
                continue
            # Evidence that built the hypothesis is its baseline, not a future
            # confirmation. Wait for a genuinely new canonical fact revision.
            if feature_observation_ref in baseline_evidence_ids:
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
            # A canonical fact revision is immutable. Repackaging the same daily or
            # weekly revision in multiple intraday packets is not new evidence and
            # must not advance persistence. Market features have no external
            # revision ref and remain independent point-in-time observations.
            if (
                feature_observation_ref is not None
                and prior is not None
                and prior.feature_observation_ref == feature_observation_ref
            ):
                continue
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
            observation_identity = (
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
            )
            if feature_observation_ref is not None:
                observation_identity = (
                    *observation_identity,
                    feature_observation_ref,
                    feature_observed_at,
                )
            observation_id = stable_id(
                "world_mechanism_observation",
                *observation_identity,
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
                feature_observation_ref=feature_observation_ref,
                feature_observed_at=feature_observed_at,
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
    return {
        selector: observation.value
        for selector, observation in packet_feature_observations(packet).items()
    }


def packet_feature_observations(
    packet: DecisionPacket,
) -> dict[str, PacketFeatureObservation]:
    """Return executable values with an identity for externally versioned facts."""

    values: dict[str, PacketFeatureObservation] = {}
    for item in packet.asset_states:
        source_ref = stable_id(
            "asset_state_observation",
            item.market_symbol,
            item.observed_at,
        )
        for field in (
            "last",
            "return_fraction",
            "realized_volatility",
            "atr",
            "spread_bps",
            "volume_ratio",
        ):
            values[f"asset_state:{item.asset}.{field}"] = PacketFeatureObservation(
                value=Decimal(str(getattr(item, field))),
                source_ref=source_ref,
                source_observed_at=item.observed_at,
            )
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
                source_observed_at, source_family = _derivative_feature_source(item, field)
                values[f"derivative_state:{item.asset}.{field}"] = (
                    PacketFeatureObservation(
                        value=Decimal(str(raw)),
                        source_ref=stable_id(
                            "derivative_feature_observation",
                            item.asset,
                            source_family,
                            source_observed_at,
                        ),
                        source_observed_at=source_observed_at,
                    )
                )
    for item in getattr(packet, "facts", ()):
        for field, value in continuous_fact_numeric_values(item).items():
            values[f"fact_state:{item.fact_type}.{field}"] = PacketFeatureObservation(
                value=value,
                source_ref=item.revision_id,
                source_observed_at=item.observed_at,
            )
    return values


def _derivative_feature_source(
    item: PacketDerivativeState,
    field: str,
) -> tuple[datetime, str]:
    if field.startswith("spot_"):
        assert item.spot_flow_observed_at is not None
        return item.spot_flow_observed_at, "spot_flow"
    if field in {
        "open_interest_change_fraction",
        "global_long_account_fraction",
        "taker_buy_sell_ratio",
    }:
        assert item.positioning_observed_at is not None
        return item.positioning_observed_at, "positioning"
    if field.startswith("last_funding_") or field.startswith("trailing_funding_"):
        return (
            item.next_funding_time - timedelta(hours=8),
            f"funding:{item.next_funding_time.isoformat()}",
        )
    return item.observed_at, "market"


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
