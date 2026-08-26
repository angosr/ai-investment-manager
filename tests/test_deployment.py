from __future__ import annotations

import pytest
from pydantic import ValidationError

from investment_manager.governance.policy import DeploymentPolicy, DeploymentStage


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
