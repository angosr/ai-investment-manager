from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_manager.execution.policy import ExecutionPolicy
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.governance.policy import DeploymentStage
from investment_manager.portfolio.policy import FrequencyPolicy
from investment_manager.settings import load_config
from investment_manager.state.policy import DecisionStatePolicy, PanelPolicy


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
    assert config.pipeline.version == "cash-observation-shadow-v1"
    assert config.temporal.namespace == "shadow-capital-20260821-v9"
    assert config.capital.enabled
    assert config.information.version == "information-intake-v26"
    assert config.information.normalizer_version == "trendradar-collector-v8"
    assert config.decision_state.version == "portfolio-state-v31"
    assert config.decision_state.official_fact_policy.version == "official-fact-v12"
    assert config.decision_state.delta_policy.version == "state-delta-v14"
    assert config.decision_state.packet_policy.version == "decision-packet-policy-v32"
    assert config.decision_state.packet_policy.schema_version == "decision-packet-v13"
    assert config.decision_state.packet_policy.maximum_packet_characters == 12_500
    assert config.market_data.funding_history_lookback_hours == 720
    assert config.assessment.mandate.capital_objective is not None
    assert config.assessment.version == "context-assessment-v27"
    assert config.assessment.mandate.version == "primary-portfolio-mandate-v6"
    regulation = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "REGULATION_LEGISLATION"
    )
    assert regulation.source_stream_ids == ("federal-register-digital-assets",)
    assert regulation.source_capabilities == {
        "federal-register-digital-assets": ("AGENCY_RULEMAKING",)
    }
    institutional = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "INSTITUTIONAL_FLOWS"
    )
    assert institutional.source_capabilities == {
        "ark-arkb-holdings": ("BTC_ETF_ARKB_HOLDINGS",),
        "bitwise-bitb-holdings": ("BTC_ETF_BITB_HOLDINGS",),
        "bykaranteli-etf-aggregate-flows": (
            "BTC_ETF_AGGREGATE_FLOW",
            "ETH_ETF_AGGREGATE_FLOW",
        ),
        "ishares-ibit-holdings": ("BTC_ETF_IBIT_HOLDINGS",),
    }
    assert institutional.maximum_poll_age_seconds == 1200
    assert config.decision_state.packet_policy.maximum_background_fact_distance_seconds == 172_800
    assert config.decision_state.packet_policy.maximum_calendar_context_distance_seconds == 604_800
    assert config.decision_state.official_fact_policy.affected_assets == (
        "BTC",
        "ETH",
    )
    assert config.assessment.mandate.required_risk_factors == (
        "US_MONETARY_POLICY",
        "US_FISCAL_LIQUIDITY",
        "US_MONETARY_LIQUIDITY",
        "US_INTEREST_RATES",
        "US_DOLLAR",
        "BTC_INSTITUTIONAL_FLOW",
        "ETH_INSTITUTIONAL_FLOW",
        "US_EQUITY_RISK_APPETITE",
        "US_HIGH_YIELD_CREDIT_RISK",
        "US_ENERGY_INFLATION",
        "BTC_INSTITUTIONAL_HOLDINGS",
        "EXTERNAL_INFORMATION",
        "MARKET_VOLATILITY",
    )
    assert config.trigger.volatility_jump_threshold == Decimal("0.01")


def test_historical_state_policy_does_not_require_future_source_rules() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.yaml")
    payload = config.decision_state.model_dump(mode="json")
    payload["delta_policy"]["rules"] = [
        item
        for item in payload["delta_policy"]["rules"]
        if item["fact_type"] != "US_DIGITAL_ASSET_RULEMAKING"
    ]

    restored = DecisionStatePolicy.model_validate(payload)

    assert all(
        item.fact_type != "US_DIGITAL_ASSET_RULEMAKING"
        for item in restored.delta_policy.rules
    )


def test_capital_can_observe_cash_without_an_active_candidate() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")

    assert config.capital.enabled
    assert config.capital.mock_candidate_authorizations == ()


def test_retired_dynamic_carry_is_read_only_and_cannot_be_reenabled() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.yaml")
    payload = config.model_dump(mode="python")
    payload["dynamic_carry_forecast"] = {
        "version": "dynamic-carry-point-in-time-v2",
        "enabled": False,
    }

    historical = type(config).model_validate(payload)
    assert historical.dynamic_carry_forecast == {
        "version": "dynamic-carry-point-in-time-v2",
        "enabled": False,
    }

    payload["dynamic_carry_forecast"]["enabled"] = True
    with pytest.raises(ValidationError, match="只允许只读解析已禁用历史身份"):
        type(config).model_validate(payload)


def test_capital_quote_alignment_must_cover_spot_freeze_interval() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["market_data"]["quote_persist_interval_ms"] = 16_000

    with pytest.raises(ValidationError, match="不得短于 Spot 冻结间隔"):
        type(config).model_validate(payload)


def test_background_fact_window_covers_the_longest_assessment_horizon() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["decision_state"]["packet_policy"]["maximum_background_fact_distance_seconds"] = 3_600

    with pytest.raises(ValidationError, match="背景事实窗口不得短于"):
        type(config).model_validate(payload)


def test_official_macro_fact_rules_cannot_be_partially_enabled() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.yaml")
    payload = config.model_dump(mode="python")
    payload["decision_state"]["delta_policy"]["rules"] = tuple(
        item
        for item in payload["decision_state"]["delta_policy"]["rules"]
        if item["fact_type"] != "NYFED_SOMA_SNAPSHOT"
    )

    with pytest.raises(ValidationError, match="必须完整启用或完整关闭"):
        type(config).model_validate(payload)

    historical = type(config).model_validate(
        payload,
        context={"historical_read_only": True},
    )
    assert len(historical.decision_state.delta_policy.rules) == len(
        config.decision_state.delta_policy.rules
    ) - 1


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
