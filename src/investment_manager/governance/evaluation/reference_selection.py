from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

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


class ReferenceSelectionPlan(FrozenModel):
    version: str = "reference-selection-plan-v1"
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
    required_layers: tuple[ReferenceEvidenceLayer, ...] = Field(min_length=1)
    stress_windows: tuple[ReferenceStressWindow, ...] = Field(min_length=1)
    candidates: tuple[ReferenceCandidateRule, ...] = Field(min_length=1)

    _utc_registered_at = field_validator("registered_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_windows_match(self):
        if not (
            self.development_start < self.development_end <= self.blind_start < self.blind_end
        ):
            raise ValueError("Reference 开发与盲测窗口必须有序且不重叠")
        if self.required_layers != tuple(sorted(set(self.required_layers))):
            raise ValueError("Reference 必需证据层必须唯一且排序")
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
    fraction: UnitInterval


class ReferenceCandidateMetrics(FrozenModel):
    annualized_nominal_return_fraction: Decimal
    annualized_real_return_fraction: Decimal
    annualized_volatility_fraction: Decimal = Field(ge=0)
    maximum_drawdown_fraction: UnitInterval
    worst_stress_loss_fraction: UnitInterval
    annualized_turnover_fraction: Decimal = Field(ge=0)
    annualized_cost_fraction: Decimal = Field(ge=0)
    risk_contributions: tuple[ReferenceRiskContribution, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def risk_contributions_are_complete(self):
        exposures = tuple(item.exposure for item in self.risk_contributions)
        if exposures != tuple(sorted(set(exposures))):
            raise ValueError("Reference 风险贡献必须按唯一经济暴露排序")
        total = sum((item.fraction for item in self.risk_contributions), Decimal("0"))
        if abs(total - Decimal("1")) > Decimal("0.000001"):
            raise ValueError("Reference 风险贡献之和必须为 1")
        return self


class ReferenceCandidateResult(FrozenModel):
    candidate_id: str = Field(min_length=1)
    status: ReferenceSelectionStatus
    reason_codes: tuple[str, ...] = ()
    development_metrics: ReferenceCandidateMetrics | None = None
    blind_metrics: ReferenceCandidateMetrics | None = None

    @model_validator(mode="after")
    def status_matches_evidence(self):
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("Reference 候选拒绝原因必须唯一且排序")
        if self.status == ReferenceSelectionStatus.QUALIFIED:
            if self.reason_codes or self.development_metrics is None or self.blind_metrics is None:
                raise ValueError("合格 Reference 候选必须有完整开发和盲测结果")
        elif not self.reason_codes:
            raise ValueError("被拒绝 Reference 候选必须保存原因")
        return self


class ReferenceSelectionArtifact(FrozenModel):
    version: str = "reference-selection-artifact-v1"
    artifact_id: str = Field(min_length=1)
    evaluated_at: datetime
    information_cutoff: date
    plan: ReferenceSelectionPlan
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[ReferenceSelectionEvidence, ...]
    results: tuple[ReferenceCandidateResult, ...] = Field(min_length=1)
    status: ReferenceSelectionStatus
    selected_candidate_id: str | None = None

    _utc_evaluated_at = field_validator("evaluated_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_selection_match(self):
        if self.plan_hash != content_hash(self.plan):
            raise ValueError("Reference 选择结果未绑定计划内容")
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
            available_layers = {item.layer for item in self.evidence}
            missing_layers = set(self.plan.required_layers) - available_layers
            if missing_layers:
                raise ValueError("合格 Reference 选择缺少预登记证据层")
            if any(
                item.last_effective_date > self.information_cutoff
                for item in self.evidence
            ):
                raise ValueError("合格 Reference 选择包含信息截止后的证据")
            if len(qualified) != 1 or self.selected_candidate_id != qualified[0]:
                raise ValueError("Reference 选择必须只有一个合格胜出候选")
        elif self.selected_candidate_id is not None or qualified:
            raise ValueError("被拒绝 Reference 选择不得声明胜出候选")
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
