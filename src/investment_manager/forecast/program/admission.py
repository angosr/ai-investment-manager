"""Resolve current-release capital admission for the active forecast cohort."""

from __future__ import annotations

from dataclasses import dataclass

from investment_manager.forecast.context.posterior_contract import (
    POSTERIOR_PRODUCER_ID,
    posterior_behavior_hash,
)
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.program.baseline import ForecastBaselineArtifact
from investment_manager.forecast.program.prior import (
    PRIOR_PRODUCER_ID,
    build_prior_targets,
)

ForecastAuthorizationIdentity = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ActiveForecastAdmissions:
    """Capital-admitted families for at most one complete joint producer."""

    prior_outcome_families: tuple[str, ...] = ()
    posterior_outcome_families: tuple[str, ...] = ()


def resolve_active_forecast_admissions(
    *,
    artifact: ForecastBaselineArtifact,
    runtime: CodexRuntimePolicy,
    world_model_behavior_id: str,
    authorization_identities: tuple[ForecastAuthorizationIdentity, ...],
) -> ActiveForecastAdmissions:
    """Match governance identities to the frozen active producer cohort.

    A joint producer is admitted as one opportunity set.  Authorizing only a
    subset of its contracts would create a portfolio universe that was never
    evaluated prospectively, while mixing producer behaviors would destroy the
    one-candidate evidence comparison.
    """

    if tuple(sorted(set(authorization_identities))) != authorization_identities:
        raise ValueError("Forecast 资本授权身份必须唯一且排序")
    targets = tuple(
        sorted(build_prior_targets(artifact), key=lambda item: item.contract.contract_id)
    )
    if not targets:
        raise ValueError("现役 Forecast cohort 缺少 prior targets")
    prior_bindings = tuple(item.binding for item in targets)
    contracts = tuple(item.contract for item in targets)
    prior_behavior_id = prior_bindings[0].producer_behavior_id
    if any(item.producer_behavior_id != prior_behavior_id for item in prior_bindings):
        raise ValueError("现役 Forecast prior targets 不属于同一联合行为")
    posterior_behavior_id = posterior_behavior_hash(
        runtime,
        contracts=contracts,
        prior_bindings=prior_bindings,
        world_model_behavior_id=world_model_behavior_id,
    )
    families = tuple(sorted(item.outcome_family_id for item in contracts))
    known = {
        (PRIOR_PRODUCER_ID, prior_behavior_id): families,
        (POSTERIOR_PRODUCER_ID, posterior_behavior_id): families,
    }
    selected: dict[tuple[str, str], set[str]] = {}
    for producer_id, behavior_id, outcome_family_id in authorization_identities:
        cohort = (producer_id, behavior_id)
        if cohort not in known or outcome_family_id not in known[cohort]:
            raise ValueError("Capital authorization 不属于现役 Forecast cohort")
        selected.setdefault(cohort, set()).add(outcome_family_id)
    if not selected:
        return ActiveForecastAdmissions()
    if len(selected) != 1:
        raise ValueError("一个 Release 只能授权一个完整 Forecast producer 行为")
    cohort, selected_families = next(iter(selected.items()))
    if selected_families != set(known[cohort]):
        raise ValueError("联合 Forecast producer 必须完整授权全部 outcome families")
    if cohort[0] == PRIOR_PRODUCER_ID:
        return ActiveForecastAdmissions(prior_outcome_families=families)
    return ActiveForecastAdmissions(posterior_outcome_families=families)


__all__ = [
    "ActiveForecastAdmissions",
    "ForecastAuthorizationIdentity",
    "resolve_active_forecast_admissions",
]
