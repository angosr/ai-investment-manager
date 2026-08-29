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
    ReferenceEconomicMetrics,
    ReferenceEvidenceLayer,
    ReferenceEvidenceRequirement,
    ReferencePlanRegistration,
    ReferenceQualificationPolicy,
    ReferenceRiskContribution,
    ReferenceSelectionCatalog,
    ReferenceSelectionEvidence,
    ReferenceSelectionStatus,
    ReferenceStressResult,
    ReferenceStressWindow,
    build_reference_candidate,
    build_reference_rejection,
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
from investment_manager.research.reference import persist_reference_evidence_manifests


def test_rejected_selection_artifact_cannot_grant_reference_policy() -> None:
    plan, candidate = _plan()
    artifact = build_reference_selection_artifact(
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
        information_cutoff=date(2026, 8, 24),
        plan=plan,
        plan_hash=content_hash(plan),
        plan_registration=_registration(plan),
        evaluator_code_version="b" * 40,
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


def test_reference_result_must_follow_durable_plan_registration() -> None:
    plan, _candidate = _plan()
    registration = _registration(plan).model_copy(
        update={"committed_at": datetime(2026, 8, 26, tzinfo=UTC)}
    )

    with pytest.raises(ValidationError, match="耐久登记后"):
        build_reference_rejection(
            plan=plan,
            plan_registration=registration,
            evaluator_code_version="b" * 40,
            evidence=(),
            evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
            information_cutoff=date(2026, 8, 24),
        )


def test_only_selected_reference_source_manifests_are_persisted(tmp_path: Path) -> None:
    source_root = tmp_path / "economic"
    source = source_root / "evidence-1" / "manifest.json"
    source.parent.mkdir(parents=True)
    raw = {"dataset_id": "evidence-1", "observations_hash": "a" * 64}
    source.write_text(json.dumps(raw), encoding="utf-8")
    selected = (
        ReferenceSelectionEvidence(
            layer=ReferenceEvidenceLayer.ECONOMIC_PROXY,
            scope="US_EQUITY",
            evidence_id="evidence-1",
            content_hash=content_hash(raw),
            first_effective_date=date(1960, 1, 1),
            last_effective_date=date(2026, 1, 1),
            observation_count=100,
        ),
    )

    targets = persist_reference_evidence_manifests(
        selected,
        economic_catalog=source_root,
        product_catalog=tmp_path / "products",
        funding_catalog=tmp_path / "funding",
        quote_catalog=tmp_path / "quotes",
        target_root=tmp_path / "durable",
    )

    assert len(targets) == 1
    assert json.loads(targets[0].read_text(encoding="utf-8")) == raw


def test_rejection_records_economic_or_evidence_failure_only(tmp_path: Path) -> None:
    plan, _candidate = _plan()
    artifact = build_reference_rejection(
        plan=plan,
        plan_registration=_registration(plan),
        evaluator_code_version="b" * 40,
        evidence=(),
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
        information_cutoff=date(2026, 8, 24),
    )

    assert artifact.status == ReferenceSelectionStatus.REJECTED
    assert artifact.selected_candidate_id is None
    assert artifact.results[0].reason_codes
    catalog = ReferenceSelectionCatalog(tmp_path)
    catalog.store(artifact)
    assert catalog.rejection(
        plan_hash=artifact.plan_hash,
        plan_registration=artifact.plan_registration,
        evaluator_code_version=artifact.evaluator_code_version,
        information_cutoff=artifact.information_cutoff,
        evidence=artifact.evidence,
        economic_development_metrics=None,
        economic_blind_metrics=None,
        economic_stress_results=(),
    ) == artifact

    complete = tuple(
        ReferenceSelectionEvidence(
            layer=requirement.layer,
            scope=requirement.scope,
            evidence_id=requirement.fixed_evidence_id or f"evidence-{index}",
            content_hash=requirement.fixed_content_hash or content_hash(index),
            first_effective_date=date(1960, 1, 1),
            last_effective_date=date(2026, 8, 24),
            observation_count=requirement.minimum_observation_count,
        )
        for index, requirement in enumerate(plan.evidence_requirements)
    )
    with pytest.raises(ValueError, match="必须运行完整费用后评价"):
        build_reference_rejection(
            plan=plan,
            plan_registration=_registration(plan),
            evaluator_code_version="b" * 40,
            evidence=complete,
            evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
            information_cutoff=date(2026, 8, 24),
        )


def test_qualified_selection_binds_exact_winner_and_policy(tmp_path: Path) -> None:
    plan, candidate = _plan()
    metrics = _metrics()
    economic_metrics = _economic_metrics()
    evidence = tuple(
        ReferenceSelectionEvidence(
            layer=requirement.layer,
            scope=requirement.scope,
            evidence_id=f"evidence-{requirement.layer.value.lower()}",
            content_hash=content_hash({"layer": requirement.layer}),
            first_effective_date=date(1960, 1, 1),
            last_effective_date=date(2026, 6, 30),
            observation_count=100,
        )
        for requirement in plan.evidence_requirements
    )
    artifact = build_reference_selection_artifact(
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
        information_cutoff=date(2026, 8, 24),
        plan=plan,
        plan_hash=content_hash(plan),
        plan_registration=_registration(plan),
        evaluator_code_version="b" * 40,
        evidence=evidence,
        results=(
            ReferenceCandidateResult(
                candidate_id=candidate.candidate_id,
                status=ReferenceSelectionStatus.QUALIFIED,
                economic_development_metrics=economic_metrics,
                economic_blind_metrics=economic_metrics,
                economic_stress_results=(
                    ReferenceStressResult(
                        stress_id="global-financial-crisis",
                        loss_fraction=Decimal("0.06"),
                    ),
                ),
                development_metrics=metrics,
                blind_metrics=metrics,
                stress_results=(
                    ReferenceStressResult(
                        stress_id="global-financial-crisis",
                        loss_fraction=Decimal("0.06"),
                    ),
                ),
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
    economic_metrics = _economic_metrics()

    with pytest.raises(ValidationError, match="缺少预登记证据"):
        build_reference_selection_artifact(
            evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
            information_cutoff=date(2026, 8, 24),
            plan=plan,
            plan_hash=content_hash(plan),
            plan_registration=_registration(plan),
            evaluator_code_version="b" * 40,
            evidence=(),
            results=(
                ReferenceCandidateResult(
                    candidate_id=candidate.candidate_id,
                    status=ReferenceSelectionStatus.QUALIFIED,
                    economic_development_metrics=economic_metrics,
                    economic_blind_metrics=economic_metrics,
                    economic_stress_results=(
                        ReferenceStressResult(
                            stress_id="global-financial-crisis",
                            loss_fraction=Decimal("0.06"),
                        ),
                    ),
                    development_metrics=metrics,
                    blind_metrics=metrics,
                    stress_results=(
                        ReferenceStressResult(
                            stress_id="global-financial-crisis",
                            loss_fraction=Decimal("0.06"),
                        ),
                    ),
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
        evidence_requirements=tuple(
            ReferenceEvidenceRequirement(
                layer=layer,
                scope=(
                    "US_CPI"
                    if layer == ReferenceEvidenceLayer.OBJECTIVE_DEFLATOR
                    else "BINANCE:SPOT:PAXGUSDT"
                ),
                minimum_observation_count=100,
                minimum_span_days=365,
            )
            for layer in sorted(ReferenceEvidenceLayer)
        ),
        stress_windows=(
            ReferenceStressWindow(
                stress_id="global-financial-crisis",
                start=date(2007, 10, 1),
                end=date(2009, 4, 1),
            ),
        ),
        qualification=ReferenceQualificationPolicy(
            minimum_annualized_real_return_fraction=Decimal("0"),
            maximum_drawdown_fraction=Decimal("0.10"),
            maximum_worst_stress_loss_fraction=Decimal("0.10"),
            maximum_annualized_turnover_fraction=Decimal("0.50"),
            maximum_annualized_cost_fraction=Decimal("0.01"),
            maximum_single_risk_contribution_fraction=Decimal("1"),
        ),
        candidates=(candidate,),
    )
    return plan, candidate


def _registration(plan) -> ReferencePlanRegistration:
    return ReferencePlanRegistration(
        repository_path="evidence/reference-selections/candidate-plan.yaml",
        commit="a" * 40,
        committed_at=datetime(2026, 8, 24, 1, tzinfo=UTC),
        plan_hash=content_hash(plan),
    )


def _metrics() -> ReferenceCandidateMetrics:
    return ReferenceCandidateMetrics(
        annualized_nominal_return_fraction=Decimal("0.03"),
        annualized_real_return_fraction=Decimal("0.01"),
        annualized_volatility_fraction=Decimal("0.02"),
        maximum_drawdown_fraction=Decimal("0.08"),
        annualized_turnover_fraction=Decimal("0.10"),
        annualized_cost_fraction=Decimal("0.00025"),
        risk_contributions=(
            ReferenceRiskContribution(
                exposure=EconomicExposure.INFLATION_SENSITIVE,
                fraction=Decimal("1"),
            ),
        ),
    )


def _economic_metrics() -> ReferenceEconomicMetrics:
    return ReferenceEconomicMetrics(
        annualized_nominal_return_fraction=Decimal("0.03"),
        annualized_real_return_fraction=Decimal("0.01"),
        annualized_volatility_fraction=Decimal("0.02"),
        maximum_drawdown_fraction=Decimal("0.08"),
        annualized_turnover_fraction=Decimal("0.10"),
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
