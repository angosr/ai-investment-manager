from __future__ import annotations

from pydantic import Field

from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.types import FrozenModel


class StageEvidence(FrozenModel):
    shadow_safety_ready: bool = False
    shadow_days: int = Field(default=0, ge=0)
    shadow_cycles: int = Field(default=0, ge=0)
    testnet_days: int = Field(default=0, ge=0)
    testnet_orders: int = Field(default=0, ge=0)
    safety_violations: int = Field(default=0, ge=0)
    duplicate_orders: int = Field(default=0, ge=0)
    unresolved_unknown_orders: int = Field(default=0, ge=0)
    reconciliation_mismatches: int = Field(default=0, ge=0)
    governance_drill_completed: bool = False
    human_approval_ref: str | None = None


class StageGateResult(FrozenModel):
    allowed: bool
    reason_codes: tuple[str, ...]


class StagePromotionGate:
    """只判断能否进入下一验证环境，不创建凭据、不切换部署。"""

    _order = (
        DeploymentStage.MOCK,
        DeploymentStage.SHADOW,
        DeploymentStage.TESTNET,
        DeploymentStage.LIVE,
    )

    def evaluate(
        self,
        current: DeploymentStage,
        target: DeploymentStage,
        evidence: StageEvidence,
    ) -> StageGateResult:
        reasons: list[str] = []
        if self._order.index(target) != self._order.index(current) + 1:
            reasons.append("STAGE_TRANSITION_MUST_BE_ADJACENT")
            return StageGateResult(allowed=False, reason_codes=tuple(reasons))
        if evidence.safety_violations:
            reasons.append("SAFETY_VIOLATIONS_PRESENT")
        if evidence.duplicate_orders:
            reasons.append("DUPLICATE_ORDERS_PRESENT")
        if evidence.reconciliation_mismatches:
            reasons.append("RECONCILIATION_MISMATCH_PRESENT")
        if target == DeploymentStage.SHADOW:
            if not evidence.shadow_safety_ready:
                reasons.append("SHADOW_SAFETY_NOT_READY")
        elif target == DeploymentStage.TESTNET:
            if not evidence.shadow_safety_ready:
                reasons.append("SHADOW_SAFETY_NOT_READY")
            if not evidence.human_approval_ref:
                reasons.append("HUMAN_APPROVAL_MISSING")
        elif target == DeploymentStage.LIVE:
            reasons.append("LIVE_ADAPTER_NOT_IMPLEMENTED")
            if evidence.testnet_days < 14:
                reasons.append("TESTNET_DURATION_TOO_SHORT")
            if evidence.testnet_orders < 100:
                reasons.append("TESTNET_SAMPLE_TOO_SMALL")
            if evidence.unresolved_unknown_orders:
                reasons.append("UNKNOWN_ORDERS_UNRESOLVED")
            if not evidence.human_approval_ref:
                reasons.append("HUMAN_APPROVAL_MISSING")
        return StageGateResult(allowed=not reasons, reason_codes=tuple(reasons))
