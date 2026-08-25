from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, UnitInterval
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.portfolio.policy import (
    EconomicExposure,
    ReferenceAllocationPolicy,
    ReferencePortfolioPolicy,
)


class ReferenceEvidenceLayer(StrEnum):
    ECONOMIC_PROXY = "ECONOMIC_PROXY"
    OBJECTIVE_DEFLATOR = "OBJECTIVE_DEFLATOR"
    PRODUCT_BARS = "PRODUCT_BARS"
    EXECUTABLE_QUOTES = "EXECUTABLE_QUOTES"
    PRODUCT_CASH_FLOWS = "PRODUCT_CASH_FLOWS"
    PRODUCT_RULES = "PRODUCT_RULES"


class ReferenceSelectionStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"


class ReferencePlanRegistration(FrozenModel):
    version: Literal["reference-plan-registration-v1"] = (
        "reference-plan-registration-v1"
    )
    repository_path: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    committed_at: datetime
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_committed_at = field_validator("committed_at")(require_utc)

    @model_validator(mode="after")
    def path_is_repository_relative(self):
        path = Path(self.repository_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Reference 计划登记路径必须位于源码仓库内")
        return self


class ReferenceSelectionEvidence(FrozenModel):
    layer: ReferenceEvidenceLayer
    scope: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_effective_date: date
    last_effective_date: date
    observation_count: int = Field(gt=0)

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.first_effective_date > self.last_effective_date:
            raise ValueError("Reference 证据日期范围非法")
        return self


class ReferenceEvidenceRequirement(FrozenModel):
    layer: ReferenceEvidenceLayer
    scope: str = Field(min_length=1)
    minimum_observation_count: int = Field(gt=0)
    minimum_span_days: int = Field(ge=0)
    fixed_evidence_id: str | None = Field(default=None, min_length=1)
    fixed_content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def fixed_identity_is_complete(self):
        if (self.fixed_evidence_id is None) != (self.fixed_content_hash is None):
            raise ValueError("Reference 固定证据必须同时声明 ID 与内容哈希")
        return self


class ReferenceStressWindow(FrozenModel):
    stress_id: str = Field(min_length=1)
    start: date
    end: date

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.start >= self.end:
            raise ValueError("Reference 压力窗口起点必须早于终点")
        return self


class ReferenceCandidateRule(FrozenModel):
    candidate_id: str = Field(min_length=1)
    allocations: tuple[ReferenceAllocationPolicy, ...] = Field(min_length=2)
    rebalance_band_fraction: UnitInterval

    @model_validator(mode="after")
    def identity_and_allocations_match(self):
        keys = tuple(item.implementation_key for item in self.allocations)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("Reference 候选分配必须唯一且排序")
        if sum(
            (item.target_exposure_fraction for item in self.allocations),
            Decimal("0"),
        ) != 1:
            raise ValueError("Reference 候选目标经济暴露之和必须为 1")
        if self.rebalance_band_fraction <= 0:
            raise ValueError("Reference 候选再平衡带必须为正")
        expected = stable_id(
            "reference_candidate",
            self.allocations,
            self.rebalance_band_fraction,
        )
        if self.candidate_id != expected:
            raise ValueError("Reference 候选身份与规则不一致")
        return self


class ReferenceQualificationPolicy(FrozenModel):
    minimum_annualized_real_return_fraction: Decimal
    maximum_drawdown_fraction: UnitInterval
    maximum_worst_stress_loss_fraction: UnitInterval
    maximum_annualized_turnover_fraction: Decimal = Field(ge=0)
    maximum_annualized_cost_fraction: Decimal = Field(ge=0)
    maximum_single_risk_contribution_fraction: UnitInterval


class ReferenceSelectionPlan(FrozenModel):
    version: str = "reference-selection-plan-v2"
    plan_id: str = Field(min_length=1)
    registered_at: datetime
    mandate_version: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    risk_policy_version: str = Field(min_length=1)
    cost_model_version: str = Field(min_length=1)
    development_start: date
    development_end: date
    blind_start: date
    blind_end: date
    evidence_requirements: tuple[ReferenceEvidenceRequirement, ...] = Field(
        min_length=1
    )
    stress_windows: tuple[ReferenceStressWindow, ...] = Field(min_length=1)
    qualification: ReferenceQualificationPolicy
    candidates: tuple[ReferenceCandidateRule, ...] = Field(min_length=1, max_length=1)

    _utc_registered_at = field_validator("registered_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_windows_match(self):
        if not (
            self.development_start < self.development_end <= self.blind_start < self.blind_end
        ):
            raise ValueError("Reference 开发与盲测窗口必须有序且不重叠")
        evidence_keys = tuple(
            (item.layer, item.scope) for item in self.evidence_requirements
        )
        if evidence_keys != tuple(sorted(set(evidence_keys))):
            raise ValueError("Reference 证据要求必须按层与作用域唯一排序")
        stress_ids = tuple(item.stress_id for item in self.stress_windows)
        if stress_ids != tuple(sorted(set(stress_ids))):
            raise ValueError("Reference 压力窗口必须按唯一 ID 排序")
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if candidate_ids != tuple(sorted(set(candidate_ids))):
            raise ValueError("Reference 候选必须按唯一 ID 排序")
        expected = stable_id(
            "reference_selection_plan",
            self.model_dump(exclude={"plan_id"}, mode="json"),
        )
        if self.plan_id != expected:
            raise ValueError("Reference 选择计划身份不一致")
        return self


class ReferenceRiskContribution(FrozenModel):
    exposure: EconomicExposure
    # Euler risk contribution can be negative when an exposure hedges the portfolio.
    fraction: Decimal


class ReferenceStressResult(FrozenModel):
    stress_id: str = Field(min_length=1)
    loss_fraction: UnitInterval


class ReferenceEconomicMetrics(FrozenModel):
    annualized_nominal_return_fraction: Decimal
    annualized_real_return_fraction: Decimal
    annualized_volatility_fraction: Decimal = Field(ge=0)
    maximum_drawdown_fraction: UnitInterval
    annualized_turnover_fraction: Decimal = Field(ge=0)
    risk_contributions: tuple[ReferenceRiskContribution, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def risk_contributions_are_complete(self):
        _validate_risk_contributions(self.risk_contributions)
        return self


class ReferenceCandidateMetrics(FrozenModel):
    annualized_nominal_return_fraction: Decimal
    annualized_real_return_fraction: Decimal
    annualized_volatility_fraction: Decimal = Field(ge=0)
    maximum_drawdown_fraction: UnitInterval
    annualized_turnover_fraction: Decimal = Field(ge=0)
    annualized_cost_fraction: Decimal = Field(ge=0)
    risk_contributions: tuple[ReferenceRiskContribution, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def risk_contributions_are_complete(self):
        _validate_risk_contributions(self.risk_contributions)
        return self


class ReferenceCandidateResult(FrozenModel):
    candidate_id: str = Field(min_length=1)
    status: ReferenceSelectionStatus
    reason_codes: tuple[str, ...] = ()
    economic_development_metrics: ReferenceEconomicMetrics | None = None
    economic_blind_metrics: ReferenceEconomicMetrics | None = None
    economic_stress_results: tuple[ReferenceStressResult, ...] = ()
    development_metrics: ReferenceCandidateMetrics | None = None
    blind_metrics: ReferenceCandidateMetrics | None = None
    stress_results: tuple[ReferenceStressResult, ...] = ()

    @model_validator(mode="after")
    def status_matches_evidence(self):
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("Reference 候选拒绝原因必须唯一且排序")
        if self.status == ReferenceSelectionStatus.QUALIFIED:
            if (
                self.reason_codes
                or self.economic_development_metrics is None
                or self.economic_blind_metrics is None
                or not self.economic_stress_results
                or self.development_metrics is None
                or self.blind_metrics is None
                or not self.stress_results
            ):
                raise ValueError("合格 Reference 候选必须有完整开发和盲测结果")
        elif not self.reason_codes:
            raise ValueError("被拒绝 Reference 候选必须保存原因")
        for values in (self.economic_stress_results, self.stress_results):
            stress_ids = tuple(item.stress_id for item in values)
            if stress_ids != tuple(sorted(set(stress_ids))):
                raise ValueError("Reference 压力结果必须按唯一 ID 排序")
        return self


class ReferenceSelectionArtifact(FrozenModel):
    version: Literal["reference-selection-artifact-v3"] = (
        "reference-selection-artifact-v3"
    )
    artifact_id: str = Field(min_length=1)
    evaluated_at: datetime
    information_cutoff: date
    plan: ReferenceSelectionPlan
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_registration: ReferencePlanRegistration
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence: tuple[ReferenceSelectionEvidence, ...]
    results: tuple[ReferenceCandidateResult, ...] = Field(min_length=1)
    status: ReferenceSelectionStatus
    selected_candidate_id: str | None = None

    _utc_evaluated_at = field_validator("evaluated_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_selection_match(self):
        if self.plan_hash != content_hash(self.plan):
            raise ValueError("Reference 选择结果未绑定计划内容")
        if self.plan_registration.plan_hash != self.plan_hash:
            raise ValueError("Reference 选择结果与计划登记内容不一致")
        if self.evaluated_at <= self.plan_registration.committed_at:
            raise ValueError("Reference 选择必须在计划耐久登记后评价")
        if self.information_cutoff > self.evaluated_at.date():
            raise ValueError("Reference 选择结果使用了评价时尚不可见的信息")
        if self.evaluated_at <= self.plan.registered_at:
            raise ValueError("Reference 选择必须在计划预登记后评价")
        if self.plan.blind_end > self.information_cutoff:
            raise ValueError("Reference 选择尚未到达冻结盲测终点")
        evidence_order = tuple(
            (item.layer, item.scope, item.evidence_id) for item in self.evidence
        )
        if evidence_order != tuple(sorted(set(evidence_order))):
            raise ValueError("Reference 选择证据必须唯一且排序")
        if any(
            item.last_effective_date > self.information_cutoff
            for item in self.evidence
        ):
            raise ValueError("Reference 选择包含信息截止后的证据")
        result_ids = tuple(item.candidate_id for item in self.results)
        plan_ids = tuple(item.candidate_id for item in self.plan.candidates)
        if result_ids != plan_ids:
            raise ValueError("Reference 选择结果必须唯一覆盖预登记候选")
        qualified = tuple(
            item.candidate_id
            for item in self.results
            if item.status == ReferenceSelectionStatus.QUALIFIED
        )
        if self.status == ReferenceSelectionStatus.QUALIFIED:
            by_requirement = {
                (item.layer, item.scope): item for item in self.evidence
            }
            missing = tuple(
                (item.layer, item.scope)
                for item in self.plan.evidence_requirements
                if (item.layer, item.scope) not in by_requirement
            )
            if missing:
                raise ValueError("合格 Reference 选择缺少预登记证据")
            for requirement in self.plan.evidence_requirements:
                observed = by_requirement[(requirement.layer, requirement.scope)]
                span_days = (
                    observed.last_effective_date - observed.first_effective_date
                ).days
                if (
                    observed.observation_count
                    < requirement.minimum_observation_count
                    or span_days < requirement.minimum_span_days
                ):
                    raise ValueError("合格 Reference 选择的证据覆盖不足")
                if requirement.fixed_evidence_id is not None and (
                    observed.evidence_id != requirement.fixed_evidence_id
                    or observed.content_hash != requirement.fixed_content_hash
                ):
                    raise ValueError("合格 Reference 选择替换了预登记固定证据")
            if len(qualified) != 1 or self.selected_candidate_id != qualified[0]:
                raise ValueError("Reference 选择必须只有一个合格胜出候选")
            result = next(item for item in self.results if item.candidate_id == qualified[0])
            if tuple(item.stress_id for item in result.economic_stress_results) != tuple(
                item.stress_id for item in self.plan.stress_windows
            ):
                raise ValueError("合格 Reference 经济压力结果未覆盖预登记窗口")
            if tuple(item.stress_id for item in result.stress_results) != tuple(
                item.stress_id for item in self.plan.stress_windows
            ):
                raise ValueError("合格 Reference 选择的压力结果未覆盖预登记窗口")
            if max(item.loss_fraction for item in result.stress_results) > (
                self.plan.qualification.maximum_worst_stress_loss_fraction
            ):
                raise ValueError("合格 Reference 选择未通过预登记压力阈值")
            for metrics in (
                result.economic_development_metrics,
                result.economic_blind_metrics,
            ):
                assert metrics is not None
                if _economic_reason_codes(metrics, self.plan.qualification):
                    raise ValueError("合格 Reference 选择未通过经济代理资格阈值")
            if max(
                item.loss_fraction for item in result.economic_stress_results
            ) > self.plan.qualification.maximum_worst_stress_loss_fraction:
                raise ValueError("合格 Reference 选择未通过经济代理压力阈值")
            for metrics in (result.development_metrics, result.blind_metrics):
                assert metrics is not None
                if _qualification_reason_codes(metrics, self.plan.qualification):
                    raise ValueError("合格 Reference 选择未通过预登记资格阈值")
        elif self.selected_candidate_id is not None or qualified:
            raise ValueError("被拒绝 Reference 选择不得声明胜出候选")
        else:
            for result in self.results:
                for metrics in (
                    result.economic_development_metrics,
                    result.economic_blind_metrics,
                ):
                    if metrics is None:
                        continue
                    missing_reasons = set(
                        _economic_reason_codes(metrics, self.plan.qualification)
                    ) - set(result.reason_codes)
                    if missing_reasons:
                        raise ValueError("Reference 拒绝结果遗漏经济阈值失败原因")
                if result.economic_stress_results and max(
                    item.loss_fraction for item in result.economic_stress_results
                ) > self.plan.qualification.maximum_worst_stress_loss_fraction and (
                    "STRESS_LOSS_LIMIT_EXCEEDED" not in result.reason_codes
                ):
                    raise ValueError("Reference 拒绝结果遗漏经济压力失败原因")
                metrics = result.blind_metrics
                if metrics is None:
                    continue
                missing_reasons = set(
                    _qualification_reason_codes(metrics, self.plan.qualification)
                ) - set(result.reason_codes)
                if result.stress_results and max(
                    item.loss_fraction for item in result.stress_results
                ) > self.plan.qualification.maximum_worst_stress_loss_fraction:
                    missing_reasons.add("STRESS_LOSS_LIMIT_EXCEEDED")
                if missing_reasons:
                    raise ValueError("Reference 拒绝结果遗漏预登记阈值失败原因")
        expected = stable_id(
            "reference_selection_artifact",
            self.model_dump(exclude={"artifact_id"}, mode="json"),
        )
        if self.artifact_id != expected:
            raise ValueError("Reference 选择制品身份不一致")
        return self


class _ReferenceSelectionEnvelope(FrozenModel):
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact: ReferenceSelectionArtifact


class ReferenceSelectionCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, artifact: ReferenceSelectionArtifact) -> Path:
        target = self._root / f"{artifact.artifact_id}.json"
        if target.exists():
            if self.load(artifact.artifact_id) != artifact:
                raise ValueError("同一 Reference 选择制品 ID 的内容不一致")
            return target
        envelope = _ReferenceSelectionEnvelope(
            artifact_hash=content_hash(artifact),
            artifact=artifact,
        )
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".reference-selection-",
            payload=envelope,
        )

    def load(self, artifact_id: str) -> ReferenceSelectionArtifact:
        return load_reference_selection_artifact(
            self._root / f"{artifact_id}.json",
            expected_artifact_id=artifact_id,
        )

    def rejection(
        self,
        *,
        plan_hash: str,
        plan_registration: ReferencePlanRegistration,
        evaluator_code_version: str,
        information_cutoff: date,
        evidence: tuple[ReferenceSelectionEvidence, ...],
        economic_development_metrics: ReferenceEconomicMetrics | None,
        economic_blind_metrics: ReferenceEconomicMetrics | None,
        economic_stress_results: tuple[ReferenceStressResult, ...],
    ) -> ReferenceSelectionArtifact | None:
        if not self._root.exists():
            return None
        for path in sorted(self._root.glob("reference_selection_artifact_*.json")):
            artifact = self.load(path.stem)
            if (
                artifact.status == ReferenceSelectionStatus.REJECTED
                and artifact.plan_hash == plan_hash
                and artifact.plan_registration == plan_registration
                and artifact.evaluator_code_version == evaluator_code_version
                and artifact.information_cutoff == information_cutoff
                and artifact.evidence == evidence
                and all(
                    item.economic_development_metrics
                    == economic_development_metrics
                    and item.economic_blind_metrics == economic_blind_metrics
                    and item.economic_stress_results == economic_stress_results
                    and item.development_metrics is None
                    and item.blind_metrics is None
                    and not item.stress_results
                    for item in artifact.results
                )
            ):
                return artifact
        return None


def load_reference_selection_plan(path: Path) -> ReferenceSelectionPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ReferenceSelectionPlan.model_validate(raw)


def load_reference_selection_artifact(
    path: Path,
    *,
    expected_artifact_id: str | None = None,
) -> ReferenceSelectionArtifact:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("artifact_hash") != content_hash(
        raw.get("artifact")
    ):
        raise ValueError("Reference 选择制品内容哈希不匹配")
    envelope = _ReferenceSelectionEnvelope.model_validate(raw)
    if (
        expected_artifact_id is not None
        and envelope.artifact.artifact_id != expected_artifact_id
    ):
        raise ValueError("Reference 选择文件与制品 ID 不一致")
    return envelope.artifact


def validate_reference_policy_selection(
    artifact: ReferenceSelectionArtifact,
    policy: ReferencePortfolioPolicy,
) -> None:
    if artifact.artifact_id != policy.selection_artifact_id:
        raise ValueError("Reference Policy 未绑定该选择制品")
    if artifact.status != ReferenceSelectionStatus.QUALIFIED:
        raise ValueError("被拒绝的 Reference 选择制品不能授予基准资格")
    if (
        artifact.plan.mandate_version != policy.mandate_version
        or artifact.plan.universe_version != policy.universe_version
    ):
        raise ValueError("Reference 选择制品与 Policy 版本作用域不一致")
    selected = next(
        item
        for item in artifact.plan.candidates
        if item.candidate_id == artifact.selected_candidate_id
    )
    if (
        selected.allocations != policy.allocations
        or selected.rebalance_band_fraction != policy.rebalance_band_fraction
    ):
        raise ValueError("Reference Policy 权重或再平衡带与胜出候选不一致")


def build_reference_candidate(
    *,
    allocations: tuple[ReferenceAllocationPolicy, ...],
    rebalance_band_fraction: Decimal,
) -> ReferenceCandidateRule:
    candidate_id = stable_id(
        "reference_candidate",
        allocations,
        rebalance_band_fraction,
    )
    return ReferenceCandidateRule(
        candidate_id=candidate_id,
        allocations=allocations,
        rebalance_band_fraction=rebalance_band_fraction,
    )


def build_reference_selection_plan(**values: object) -> ReferenceSelectionPlan:
    provisional = ReferenceSelectionPlan.model_construct(plan_id="pending", **values)
    plan_id = stable_id(
        "reference_selection_plan",
        provisional.model_dump(exclude={"plan_id"}, mode="json"),
    )
    return ReferenceSelectionPlan(plan_id=plan_id, **values)


def build_reference_selection_artifact(**values: object) -> ReferenceSelectionArtifact:
    provisional = ReferenceSelectionArtifact.model_construct(
        artifact_id="pending",
        **values,
    )
    artifact_id = stable_id(
        "reference_selection_artifact",
        provisional.model_dump(exclude={"artifact_id"}, mode="json"),
    )
    return ReferenceSelectionArtifact(artifact_id=artifact_id, **values)


def build_reference_rejection(
    *,
    plan: ReferenceSelectionPlan,
    plan_registration: ReferencePlanRegistration,
    evaluator_code_version: str,
    evidence: tuple[ReferenceSelectionEvidence, ...],
    evaluated_at: datetime,
    information_cutoff: date,
    economic_development_metrics: ReferenceEconomicMetrics | None = None,
    economic_blind_metrics: ReferenceEconomicMetrics | None = None,
    economic_stress_results: tuple[ReferenceStressResult, ...] = (),
) -> ReferenceSelectionArtifact:
    """Record a failed candidate without any path to self-qualification."""

    by_key = {(item.layer, item.scope): item for item in evidence}
    reasons: list[str] = []
    for requirement in plan.evidence_requirements:
        key = (requirement.layer, requirement.scope)
        observed = by_key.get(key)
        prefix = f"{requirement.layer.value}::{requirement.scope}"
        if observed is None:
            reasons.append(f"EVIDENCE_MISSING::{prefix}")
            continue
        if requirement.fixed_evidence_id is not None and (
            observed.evidence_id != requirement.fixed_evidence_id
            or observed.content_hash != requirement.fixed_content_hash
        ):
            reasons.append(f"FIXED_EVIDENCE_MISMATCH::{prefix}")
        if observed.observation_count < requirement.minimum_observation_count:
            reasons.append(f"OBSERVATION_COUNT_INSUFFICIENT::{prefix}")
        if (
            observed.last_effective_date - observed.first_effective_date
        ).days < requirement.minimum_span_days:
            reasons.append(f"HISTORY_SPAN_INSUFFICIENT::{prefix}")
    for metrics in (economic_development_metrics, economic_blind_metrics):
        if metrics is not None:
            reasons.extend(_economic_reason_codes(metrics, plan.qualification))
    if economic_stress_results and max(
        item.loss_fraction for item in economic_stress_results
    ) > plan.qualification.maximum_worst_stress_loss_fraction:
        reasons.append("STRESS_LOSS_LIMIT_EXCEEDED")
    if not reasons:
        raise ValueError("Reference 没有拒绝理由，必须运行完整费用后评价")
    result = ReferenceCandidateResult(
        candidate_id=plan.candidates[0].candidate_id,
        status=ReferenceSelectionStatus.REJECTED,
        reason_codes=tuple(sorted(set(reasons))),
        economic_development_metrics=economic_development_metrics,
        economic_blind_metrics=economic_blind_metrics,
        economic_stress_results=economic_stress_results,
    )
    return build_reference_selection_artifact(
        evaluated_at=evaluated_at,
        information_cutoff=information_cutoff,
        plan=plan,
        plan_hash=content_hash(plan),
        plan_registration=plan_registration,
        evaluator_code_version=evaluator_code_version,
        evidence=evidence,
        results=(result,),
        status=ReferenceSelectionStatus.REJECTED,
    )


def _qualification_reason_codes(
    metrics: ReferenceCandidateMetrics,
    policy: ReferenceQualificationPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if (
        metrics.annualized_real_return_fraction
        < policy.minimum_annualized_real_return_fraction
    ):
        reasons.append("REAL_RETURN_BELOW_MINIMUM")
    if metrics.maximum_drawdown_fraction > policy.maximum_drawdown_fraction:
        reasons.append("DRAWDOWN_LIMIT_EXCEEDED")
    if (
        metrics.annualized_turnover_fraction
        > policy.maximum_annualized_turnover_fraction
    ):
        reasons.append("TURNOVER_LIMIT_EXCEEDED")
    if metrics.annualized_cost_fraction > policy.maximum_annualized_cost_fraction:
        reasons.append("COST_LIMIT_EXCEEDED")
    if max(item.fraction for item in metrics.risk_contributions) > (
        policy.maximum_single_risk_contribution_fraction
    ):
        reasons.append("RISK_CONCENTRATION_LIMIT_EXCEEDED")
    return tuple(reasons)


def _economic_reason_codes(
    metrics: ReferenceEconomicMetrics,
    policy: ReferenceQualificationPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if (
        metrics.annualized_real_return_fraction
        < policy.minimum_annualized_real_return_fraction
    ):
        reasons.append("REAL_RETURN_BELOW_MINIMUM")
    if metrics.maximum_drawdown_fraction > policy.maximum_drawdown_fraction:
        reasons.append("DRAWDOWN_LIMIT_EXCEEDED")
    if (
        metrics.annualized_turnover_fraction
        > policy.maximum_annualized_turnover_fraction
    ):
        reasons.append("TURNOVER_LIMIT_EXCEEDED")
    if max(item.fraction for item in metrics.risk_contributions) > (
        policy.maximum_single_risk_contribution_fraction
    ):
        reasons.append("RISK_CONCENTRATION_LIMIT_EXCEEDED")
    return tuple(reasons)


def _validate_risk_contributions(
    contributions: tuple[ReferenceRiskContribution, ...],
) -> None:
    exposures = tuple(item.exposure for item in contributions)
    if exposures != tuple(sorted(set(exposures))):
        raise ValueError("Reference 风险贡献必须按唯一经济暴露排序")
    total = sum((item.fraction for item in contributions), Decimal("0"))
    if abs(total - Decimal("1")) > Decimal("0.000001"):
        raise ValueError("Reference 风险贡献之和必须为 1")
