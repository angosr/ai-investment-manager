from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_manager.execution.policy import ExecutionPolicy
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.governance.policy import DeploymentStage
from investment_manager.portfolio.policy import FrequencyPolicy
from investment_manager.settings import load_config
from investment_manager.state.policy import PanelPolicy


def test_shadow_config_inherits_single_baseline_without_enabling_orders() -> None:
    root = Path(__file__).resolve().parents[1]

    config = load_config(root / "config" / "investment-manager.shadow.yaml")

    assert config.deployment.stage == DeploymentStage.SHADOW
    assert config.deployment.shadow_market_data_enabled
    assert not config.deployment.testnet_order_submission_enabled
    assert not config.deployment.live_order_submission_enabled
    assert config.deployment.credential_profile is None
    assert not config.strategy.enabled
    assert not config.codex_runtime.enabled
    assert config.panel.max_characters == 12_000
    assert config.codex_runtime.maximum_prompt_characters == 16_000
    assert config.pipeline.ai_mode.value == "OFF"
    assert config.pipeline.version == "carry-capital-shadow-v7"
    assert config.temporal.namespace == "shadow-capital-20260821-v7"
    assert config.capital.enabled
    assert config.information.version == "information-intake-v10"
    assert config.information.normalizer_version == "trendradar-collector-v7"
    assert config.decision_state.version == "portfolio-state-v2"
    assert config.decision_state.official_fact_policy.version == "fed-official-fact-v2"
    assert config.decision_state.delta_policy.version == "state-delta-v4"
    assert config.decision_state.packet_policy.version == "decision-packet-policy-v6"
    assert (
        config.decision_state.packet_policy.maximum_background_fact_distance_seconds
        == 172_800
    )
    assert config.decision_state.official_fact_policy.affected_assets == (
        "BTC",
        "ETH",
    )
    assert config.assessment.mandate.required_risk_factors == (
        "EXTERNAL_INFORMATION",
        "MARKET_VOLATILITY",
        "US_MONETARY_POLICY",
    )
    assert config.trigger.volatility_jump_threshold == Decimal("0.01")


def test_capital_sizing_cannot_drift_from_released_carry_evidence() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["capital"]["risk"]["maximum_gross_exposure_fraction"] = Decimal("0.29")

    with pytest.raises(ValidationError, match="仓位尺寸必须完全一致"):
        type(config).model_validate(payload)


def test_background_fact_window_covers_the_longest_assessment_horizon() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["decision_state"]["packet_policy"][
        "maximum_background_fact_distance_seconds"
    ] = 3_600

    with pytest.raises(ValidationError, match="背景事实窗口不得短于"):
        type(config).model_validate(payload)


def test_testnet_config_uses_the_same_official_environment_for_market_and_orders() -> None:
    root = Path(__file__).resolve().parents[1]

    config = load_config(root / "config" / "investment-manager.testnet.yaml")

    assert config.deployment.stage == DeploymentStage.TESTNET
    assert config.market_data.rest_base_url == "https://testnet.binance.vision"
    assert config.market_data.websocket_base_url == "wss://stream.testnet.binance.vision"
    assert not config.capital.enabled


def test_config_inheritance_rejects_cycles(tmp_path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("extends: second.yaml\n", encoding="utf-8")
    second.write_text("extends: first.yaml\n", encoding="utf-8")

    with pytest.raises(ValueError, match="存在循环"):
        load_config(first)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("minimum_net_edge_bps", Decimal("-1")),
        ("latency_bps", Decimal("-1")),
        ("adverse_selection_bps", Decimal("-1")),
        ("uncertainty_buffer_bps", Decimal("-1")),
    ),
)
def test_frequency_policy_rejects_negative_gate_or_risk_buffers(field, value) -> None:
    with pytest.raises(ValidationError):
        FrequencyPolicy(version="invalid-frequency", **{field: value})


@pytest.mark.parametrize("field", ("fee_bps", "market_slippage_bps"))
def test_execution_policy_rejects_negative_trade_costs(field) -> None:
    with pytest.raises(ValidationError):
        ExecutionPolicy(version="invalid-execution", **{field: Decimal("-1")})


def test_ai_input_budgets_cannot_regress_to_unbounded_raw_context() -> None:
    with pytest.raises(ValidationError):
        PanelPolicy(version="oversized-panel", max_characters=12_001)
    with pytest.raises(ValidationError):
        CodexRuntimePolicy(
            version="oversized-prompt",
            expected_cli_version="codex-cli test",
            model="test-model",
            reasoning_effort="low",
            maximum_prompt_characters=16_001,
        )


def test_enabled_codex_runtime_requires_frozen_binary_digest() -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        CodexRuntimePolicy(
            version="missing-binary-digest",
            enabled=True,
            isolation_verified=True,
            expected_cli_version="codex-cli test",
            model="test-model",
            reasoning_effort="low",
        )
