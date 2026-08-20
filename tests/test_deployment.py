from __future__ import annotations

import pytest
from pydantic import ValidationError

from investment_manager.config import DeploymentPolicy, DeploymentStage
from investment_manager.deployment import StageEvidence, StagePromotionGate


def test_default_deployment_is_mock_without_credentials_or_order_authority(app_config) -> None:
    deployment = app_config.deployment

    assert deployment.stage == DeploymentStage.MOCK
    assert not deployment.shadow_market_data_enabled
    assert not deployment.testnet_order_submission_enabled
    assert not deployment.live_order_submission_enabled
    assert deployment.credential_profile is None


def test_config_cannot_enable_live_or_smuggle_order_permission_into_shadow() -> None:
    with pytest.raises(ValidationError, match="LIVE 适配器未实现"):
        DeploymentPolicy(
            version="deployment-v2",
            stage=DeploymentStage.LIVE,
            shadow_market_data_enabled=True,
            live_order_submission_enabled=True,
            credential_profile="secret-profile",
            manual_approval_ref="approval-1",
        )

    with pytest.raises(ValidationError, match="SHADOW"):
        DeploymentPolicy(
            version="deployment-v2",
            stage=DeploymentStage.SHADOW,
            shadow_market_data_enabled=True,
            testnet_order_submission_enabled=True,
        )


def test_stage_gate_forbids_skipping_and_requires_real_shadow_evidence() -> None:
    gate = StagePromotionGate()

    skipped = gate.evaluate(
        DeploymentStage.MOCK,
        DeploymentStage.TESTNET,
        StageEvidence(shadow_safety_ready=True),
    )
    assert not skipped.allowed
    assert skipped.reason_codes == ("STAGE_TRANSITION_MUST_BE_ADJACENT",)

    insufficient = gate.evaluate(
        DeploymentStage.SHADOW,
        DeploymentStage.TESTNET,
        StageEvidence(shadow_days=2, shadow_cycles=20),
    )
    assert not insufficient.allowed
    assert set(insufficient.reason_codes) == {
        "SHADOW_SAFETY_NOT_READY",
        "HUMAN_APPROVAL_MISSING",
    }

    eligible = gate.evaluate(
        DeploymentStage.SHADOW,
        DeploymentStage.TESTNET,
        StageEvidence(
            shadow_safety_ready=True,
            human_approval_ref="approval-1",
        ),
    )
    assert eligible.allowed


def test_live_is_unconditionally_blocked_until_adapter_is_separately_implemented() -> None:
    result = StagePromotionGate().evaluate(
        DeploymentStage.TESTNET,
        DeploymentStage.LIVE,
        StageEvidence(
            testnet_days=30,
            testnet_orders=500,
            human_approval_ref="approval-2",
        ),
    )

    assert not result.allowed
    assert "LIVE_ADAPTER_NOT_IMPLEMENTED" in result.reason_codes


def test_shadow_gate_requires_only_explicit_shadow_safety_evidence() -> None:
    blocked = StagePromotionGate().evaluate(
        DeploymentStage.MOCK,
        DeploymentStage.SHADOW,
        StageEvidence(),
    )
    allowed = StagePromotionGate().evaluate(
        DeploymentStage.MOCK,
        DeploymentStage.SHADOW,
        StageEvidence(shadow_safety_ready=True),
    )

    assert blocked.reason_codes == ("SHADOW_SAFETY_NOT_READY",)
    assert allowed.allowed
