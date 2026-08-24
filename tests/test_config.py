from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_manager.execution.policy import ExecutionPolicy
from investment_manager.forecast.context.estimate import context_forecast_behavior_hash
from investment_manager.forecast.context.producer import context_spot_forecast_contract
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
    assert config.codex_runtime.enabled
    assert config.codex_runtime.isolation_verified
    assert config.panel.max_characters == 12_000
    assert config.codex_runtime.maximum_prompt_characters == 16_000
    assert config.pipeline.ai_mode.value == "OFF"
    assert config.pipeline.version == "world-forecast-spot-capital-shadow-v31"
    assert config.temporal.namespace == "shadow-world-forecast-capital-v1"
    assert config.temporal.version == "temporal-analysis-v3"
    assert config.temporal.activity_start_to_close_seconds == 890
    assert config.temporal.activity_schedule_to_close_seconds == 900
    assert config.shadow.analysis_deadline_seconds == 900
    assert config.codex_runtime.version == "codex-runtime-v8"
    assert config.codex_runtime.timeout_seconds == 420
    assert config.codex_runtime.lease_ttl_seconds == 450
    assert config.capital.enabled
    assert config.information.version == "information-intake-v30"
    assert config.information.normalizer_version == "trendradar-collector-v9"
    assert config.decision_state.version == "portfolio-state-v36"
    assert config.decision_state.official_fact_policy.version == "official-fact-v15"
    assert config.decision_state.delta_policy.version == "state-delta-v15"
    assert config.decision_state.packet_policy.version == "decision-packet-policy-v39"
    assert config.decision_state.packet_policy.schema_version == "decision-packet-v17"
    assert config.decision_state.packet_policy.maximum_facts == 20
    assert config.decision_state.packet_policy.maximum_fact_characters == 7_000
    assert config.decision_state.packet_policy.maximum_characters_per_fact == 1_200
    assert config.decision_state.packet_policy.maximum_packet_characters == 12_500
    assert config.market_data.funding_history_lookback_hours == 720
    assert config.assessment.version == "context-assessment-v35"
    assert config.assessment.review_trigger_symbol == "BTCUSDT"
    assert config.assessment.mandate.version == "primary-portfolio-mandate-v9"
    regulation = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "REGULATION_LEGISLATION"
    )
    assert regulation.source_stream_ids == ("federal-register-digital-assets",)
    assert regulation.source_capabilities == {
        "federal-register-digital-assets": ("AGENCY_RULEMAKING",)
    }
    fiscal = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "FISCAL_DEBT"
    )
    assert fiscal.source_capabilities["treasury-auction-results"] == (
        "AUCTION_ABSORPTION",
        "DEBT_ISSUANCE",
    )
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
        item.fact_type != "US_DIGITAL_ASSET_RULEMAKING" for item in restored.delta_policy.rules
    )


def test_shadow_has_one_explicit_context_candidate() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")

    assert config.capital.enabled
    assert config.assessment.enabled
    assert config.capital.context_forecast is not None
    assert config.capital.context_forecast.enabled
    assert len(config.capital.mock_candidate_authorizations) == 1
    authorization = config.capital.mock_candidate_authorizations[0]
    assert authorization.producer_behavior_id == (
        config.capital.context_forecast.producer_behavior_id
    )
    instrument = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.key == config.capital.context_forecast.target_instrument_key
    )
    contract = context_spot_forecast_contract(
        policy=config.capital.context_forecast,
        instrument=instrument,
        cost_semantics_version=config.capital.decision.cost_model_version,
    )
    assert config.capital.context_forecast.producer_behavior_id == (
        context_forecast_behavior_hash(
            config.codex_runtime,
            config.capital.context_forecast,
            contract,
        )
    )


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


def _enabled_assessment_payload() -> tuple[type, dict]:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["assessment"]["enabled"] = True
    for account in payload["codex_accounts"]["accounts"][:3]:
        account["enabled"] = True
    payload["codex_runtime"].update(
        {
            "enabled": True,
            "isolation_verified": True,
            "expected_binary_sha256": "a" * 64,
        }
    )
    return type(config), payload


def test_enabled_assessment_requires_lease_to_cover_one_codex_call() -> None:
    config_type, payload = _enabled_assessment_payload()
    payload["codex_runtime"]["lease_ttl_seconds"] = payload["codex_runtime"]["timeout_seconds"]

    with pytest.raises(ValidationError, match="账号租约必须长于"):
        config_type.model_validate(payload)


def test_enabled_assessment_activity_covers_capacity_and_account_failover() -> None:
    config_type, payload = _enabled_assessment_payload()
    enabled_accounts = sum(item["enabled"] for item in payload["codex_accounts"]["accounts"])
    attempts = min(
        enabled_accounts,
        1 + payload["codex_runtime"]["max_account_switches"],
    )
    payload["temporal"]["activity_start_to_close_seconds"] = (
        enabled_accounts * payload["codex_runtime"]["capacity_probe_timeout_seconds"]
        + attempts * payload["codex_runtime"]["timeout_seconds"]
    )

    with pytest.raises(ValidationError, match="容量探测和账号故障切换"):
        config_type.model_validate(payload)


def test_enabled_assessment_deadline_covers_activity_schedule() -> None:
    config_type, payload = _enabled_assessment_payload()
    payload["shadow"]["analysis_deadline_seconds"] = (
        payload["temporal"]["activity_schedule_to_close_seconds"] - 1
    )

    with pytest.raises(ValidationError, match="分析截止时间必须覆盖"):
        config_type.model_validate(payload)
