from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_manager.governance.evaluation.reference_selection import (
    ReferenceCandidateMetrics,
    ReferenceCandidateResult,
    ReferenceEvidenceLayer,
    ReferenceRiskContribution,
    ReferenceSelectionCatalog,
    ReferenceSelectionEvidence,
    ReferenceSelectionStatus,
    ReferenceStressWindow,
    build_reference_candidate,
    build_reference_selection_artifact,
    build_reference_selection_plan,
    validate_reference_policy_selection,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.portfolio.policy import (
    EconomicExposure,
    ReferenceAllocationPolicy,
    ReferencePortfolioPolicy,
)


def test_rejected_selection_artifact_cannot_grant_reference_policy() -> None:
    plan, candidate = _plan()
    artifact = build_reference_selection_artifact(
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
        information_cutoff=date(2026, 8, 24),
        plan=plan,
        plan_hash=content_hash(plan),
        evidence=(),
        results=(
            ReferenceCandidateResult(
                candidate_id=candidate.candidate_id,
                status=ReferenceSelectionStatus.REJECTED,
                reason_codes=("EXECUTABLE_QUOTES_MISSING",),
            ),
        ),
        status=ReferenceSelectionStatus.REJECTED,
    )
    policy = _policy(candidate, artifact.artifact_id)

    with pytest.raises(ValueError, match="被拒绝"):
        validate_reference_policy_selection(artifact, policy)


def test_qualified_selection_binds_exact_winner_and_policy(tmp_path: Path) -> None:
    plan, candidate = _plan()
    metrics = _metrics()
    evidence = tuple(
        ReferenceSelectionEvidence(
            layer=layer,
            scope=(
                "US_CPI"
                if layer == ReferenceEvidenceLayer.OBJECTIVE_DEFLATOR
                else "BINANCE:SPOT:PAXGUSDT"
            ),
            evidence_id=f"evidence-{layer.value.lower()}",
            content_hash=content_hash({"layer": layer}),
            first_effective_date=date(1960, 1, 1),
            last_effective_date=date(2026, 6, 30),
            observation_count=100,
        )
        for layer in plan.required_layers
    )
    artifact = build_reference_selection_artifact(
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
        information_cutoff=date(2026, 8, 24),
        plan=plan,
        plan_hash=content_hash(plan),
        evidence=evidence,
        results=(
            ReferenceCandidateResult(
                candidate_id=candidate.candidate_id,
                status=ReferenceSelectionStatus.QUALIFIED,
                development_metrics=metrics,
                blind_metrics=metrics,
            ),
        ),
        status=ReferenceSelectionStatus.QUALIFIED,
        selected_candidate_id=candidate.candidate_id,
    )
    policy = _policy(candidate, artifact.artifact_id)

    validate_reference_policy_selection(artifact, policy)
    catalog = ReferenceSelectionCatalog(tmp_path)
    target = catalog.store(artifact)
    assert catalog.load(artifact.artifact_id) == artifact

    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["artifact"]["information_cutoff"] = "2026-08-23"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(artifact.artifact_id)

    changed = policy.model_copy(
        update={"rebalance_band_fraction": Decimal("0.04")}
    )
    with pytest.raises(ValueError, match="权重或再平衡带"):
        validate_reference_policy_selection(artifact, changed)


def test_qualified_selection_requires_every_pre_registered_evidence_layer() -> None:
    plan, candidate = _plan()
    metrics = _metrics()

    with pytest.raises(ValidationError, match="缺少预登记证据层"):
        build_reference_selection_artifact(
            evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
            information_cutoff=date(2026, 8, 24),
            plan=plan,
            plan_hash=content_hash(plan),
            evidence=(),
            results=(
                ReferenceCandidateResult(
                    candidate_id=candidate.candidate_id,
                    status=ReferenceSelectionStatus.QUALIFIED,
                    development_metrics=metrics,
                    blind_metrics=metrics,
                ),
            ),
            status=ReferenceSelectionStatus.QUALIFIED,
            selected_candidate_id=candidate.candidate_id,
        )


def _plan():
    candidate = build_reference_candidate(
        allocations=(
            ReferenceAllocationPolicy(
                implementation_key="BINANCE:SPOT:PAXGUSDT",
                target_exposure_fraction=Decimal("0.15"),
            ),
            ReferenceAllocationPolicy(
                implementation_key="CASH:USDT",
                target_exposure_fraction=Decimal("0.85"),
            ),
        ),
        rebalance_band_fraction=Decimal("0.05"),
    )
    plan = build_reference_selection_plan(
        registered_at=datetime(2026, 8, 24, tzinfo=UTC),
        mandate_version="mandate-v1",
        universe_version="universe-v1",
        risk_policy_version="risk-v1",
        cost_model_version="cost-v1",
        development_start=date(1960, 1, 1),
        development_end=date(2015, 1, 1),
        blind_start=date(2016, 1, 1),
        blind_end=date(2026, 8, 1),
        required_layers=tuple(sorted(ReferenceEvidenceLayer)),
        stress_windows=(
            ReferenceStressWindow(
                stress_id="global-financial-crisis",
                start=date(2007, 10, 1),
                end=date(2009, 4, 1),
            ),
        ),
        candidates=(candidate,),
    )
    return plan, candidate


def _metrics() -> ReferenceCandidateMetrics:
    return ReferenceCandidateMetrics(
        annualized_nominal_return_fraction=Decimal("0.03"),
        annualized_real_return_fraction=Decimal("0.01"),
        annualized_volatility_fraction=Decimal("0.02"),
        maximum_drawdown_fraction=Decimal("0.08"),
        worst_stress_loss_fraction=Decimal("0.06"),
        annualized_turnover_fraction=Decimal("0.10"),
        annualized_cost_fraction=Decimal("0.00025"),
        risk_contributions=(
            ReferenceRiskContribution(
                exposure=EconomicExposure.INFLATION_SENSITIVE,
                fraction=Decimal("1"),
            ),
        ),
    )


def _policy(candidate, artifact_id: str) -> ReferencePortfolioPolicy:
    return ReferencePortfolioPolicy(
        version="reference-v1",
        mandate_version="mandate-v1",
        universe_version="universe-v1",
        selection_artifact_id=artifact_id,
        allocations=candidate.allocations,
        rebalance_band_fraction=candidate.rebalance_band_fraction,
    )
