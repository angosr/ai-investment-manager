from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_manager.entrypoints.cli.support import observed_market_instruments
from investment_manager.forecast.context.contract import ASSESS_INSTRUCTIONS
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.governance.policy import DeploymentStage
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.policy import MarketDataPolicy
from investment_manager.platform.database import build_engine
from investment_manager.portfolio.policy import (
    EconomicExposure,
    MandateStatus,
)
from investment_manager.settings import AppConfig, load_config
from investment_manager.state.decision.service import assemble_decision_packet_preparation
from investment_manager.state.policy import DecisionStatePolicy


def test_shadow_config_inherits_single_baseline_without_enabling_orders() -> None:
    root = Path(__file__).resolve().parents[1]

    config = load_config(root / "config" / "investment-manager.shadow.yaml")

    assert config.deployment.stage == DeploymentStage.SHADOW
    assert config.deployment.shadow_market_data_enabled
    assert not config.deployment.testnet_order_submission_enabled
    assert not config.deployment.live_order_submission_enabled
    assert config.deployment.credential_profile is None
    assert not hasattr(config, "strategy")
    assert not hasattr(config, "calibration")
    assert not hasattr(config, "frequency")
    assert not hasattr(config, "risk")
    assert not hasattr(config, "execution")
    assert not hasattr(config, "reconciliation")
    assert not hasattr(config, "proposal")
    assert config.codex_runtime.enabled
    assert config.codex_runtime.isolation_verified
    assert config.codex_runtime.maximum_prompt_characters == 16_000
    assess_prompt_overhead = len("\n".join((*ASSESS_INSTRUCTIONS, "decision_packet_json="))) + 1
    assert (
        config.decision_state.packet_policy.maximum_packet_characters + assess_prompt_overhead
        <= config.codex_runtime.maximum_prompt_characters
    )
    assert config.pipeline.version == "world-capital-shadow-v88"
    assert config.temporal.namespace == "shadow-world-forecast-capital-v1"
    assert config.temporal.version == "temporal-analysis-v5"
    assert config.temporal.activity_start_to_close_seconds == 890
    assert config.temporal.activity_schedule_to_close_seconds == 900
    assert config.shadow.analysis_deadline_seconds == 900
    assert config.codex_runtime.version == "codex-runtime-v11"
    assert config.codex_runtime.timeout_seconds == 420
    assert config.codex_runtime.lease_ttl_seconds == 450
    assert config.capital.enabled
    assert config.capital.version == "total-portfolio-capital-v81"
    assert config.capital.mandate.portfolio_id == "primary"
    assert config.capital.mandate.status == MandateStatus.PROVISIONAL
    assert config.capital.mandate.objective == "REAL_CAPITAL_GROWTH"
    assert config.capital.investable_universe.version == ("binance-shadow-investable-v9")
    assert config.capital.reference_policy is None
    assert tuple(
        item.instrument_key for item in config.capital.investable_universe.instruments
    ) == (
        "BINANCE:TRADFI_PERPETUAL:SPYUSDT",
        "BINANCE:USD_M_PERPETUAL:BTCUSDT",
        "BINANCE:USD_M_PERPETUAL:PAXGUSDT",
    )
    assert tuple(item.key for item in config.capital.forecast_reference_instruments) == (
        "BINANCE:SPOT:BTCUSDT",
        "BINANCE:SPOT:PAXGUSDT",
    )
    assert tuple(
        (item.economic_exposure_id, item.instrument_keys)
        for item in config.capital.product_payoff_policies
    ) == (
        ("CRYPTO_NETWORK:BTC:USDT", ("BINANCE:USD_M_PERPETUAL:BTCUSDT",)),
        (
            "INFLATION_SENSITIVE:PAXG:USDT",
            ("BINANCE:USD_M_PERPETUAL:PAXGUSDT",),
        ),
    )
    assert config.capital.decision.version == "portfolio-net-edge-v17"
    assert config.information.version == "information-intake-v47"
    assert config.information.normalizer_version == "trendradar-collector-v12"
    assert config.information.economic_release_calendar_poll_seconds == 21_600
    assert config.information.economic_release_actual_poll_seconds == 15
    assert config.information.economic_release_actual_deadline_seconds == 900
    assert config.information.economic_release_actual_recovery_lookback_seconds == 14_400
    assert "bea-economic-releases" not in {
        item.stream_id for item in config.information.official_event_feeds
    }
    assert all(item.entry_path_pattern for item in config.information.official_event_feeds)
    assert {
        item.stream_id
        for item in config.information.official_event_feeds
        if item.immediate_review_eligible
    } == {"fed-speeches", "fed-testimony"}
    assert config.information.official_metric_slow_poll_seconds == 21_600
    assert config.decision_state.version == "portfolio-state-v56"
    assert config.decision_state.official_fact_policy.version == "official-fact-v21"
    assert config.decision_state.delta_policy.version == "state-delta-v19"
    assert config.decision_state.packet_policy.version == "decision-packet-policy-v59"
    assert config.decision_state.packet_policy.schema_version == "decision-packet-v21"
    assert config.decision_state.packet_policy.maximum_facts == 20
    assert config.decision_state.packet_policy.maximum_fact_characters == 7_000
    assert config.decision_state.packet_policy.maximum_characters_per_fact == 1_200
    assert config.decision_state.packet_policy.maximum_characters_per_intelligence_event == 800
    assert config.decision_state.packet_policy.maximum_packet_characters == 12_750
    assert config.decision_state.packet_policy.maximum_market_age_seconds == 180
    assert config.market_data.funding_history_lookback_hours == 720
    assert config.market_data.version == "binance-public-shadow-v18"
    assert config.market_data.symbols == (
        "BTCUSDT",
        "ETHUSDT",
        "PAXGUSDT",
        "SPYBUSDT",
    )
    assert config.market_data.reference_price_symbols == ("SPYBUSDT",)
    assert config.analysis_symbols == ("BTCUSDT", "ETHUSDT", "PAXGUSDT")
    observed = {item.key for item in observed_market_instruments(config)}
    assert "BINANCE:SPOT:SPYBUSDT" in observed
    assert "BINANCE:SPOT:SPYBUSDT" not in {
        item.instrument.key for item in config.capital.execution_specs
    }
    assert config.market_data.perpetual_quote_poll_seconds == 5
    assert config.market_data.perpetual_poll_seconds == 120
    assert tuple(item.symbol for item in config.market_data.perpetual_instruments) == (
        "SPYUSDT",
        "XAUUSDT",
        "BTCUSDT",
        "ETHUSDT",
        "PAXGUSDT",
    )
    assert (
        config.market_data.perpetual_quote_poll_seconds
        <= config.market_data.maximum_cross_market_quote_skew_seconds
    )
    assert config.market_data.cross_venue_spot is not None
    assert config.market_data.cross_venue_spot.version == "cross-venue-spot-v1"
    assert config.market_data.cross_venue_spot.poll_seconds == 10
    assert config.market_data.cross_venue_spot.maximum_age_seconds == 30
    assert tuple(item.symbol for item in config.market_data.cross_venue_spot.products) == (
        "BTCUSDT",
        "ETHUSDT",
    )
    assert config.assessment.version == "context-assessment-v59"
    assert config.outcome_evaluation.version == "typed-outcome-settlement-v59"
    assert config.outcome_evaluation.forecast_prior.enabled
    assert config.outcome_evaluation.forecast_prior.artifact_id == (
        "forecast_baseline_7edf2cf090b47cdad2e5"
    )
    assert config.assessment.review_trigger_symbol == "BTCUSDT"
    assert config.trigger.version == "analysis-trigger-v36"
    assert config.assessment.mandate.version == "primary-portfolio-mandate-v15"
    assert tuple(
        (item.target_asset, item.reference_instrument_key)
        for item in config.assessment.mandate.observation_references
    ) == (("PAXG", "BINANCE:TRADFI_PERPETUAL:XAUUSDT"),)
    assert config.assessment.mandate.observation_references[0].reference_instrument_key not in {
        item.instrument.key for item in config.capital.execution_specs
    }
    assert tuple(item.key for item in config.assessment.mandate.mandate_exposures) == (
        ("CRYPTO_NETWORK", "BTC"),
        ("INFLATION_SENSITIVE", "PAXG"),
        ("US_EQUITY", "SPY"),
    )
    regulation = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "REGULATION_LEGISLATION"
    )
    assert tuple(source.stream_id for source in regulation.sources) == (
        "federal-register-digital-assets",
        "ofac-recent-actions",
        "treasury-press-releases",
    )
    assert {source.stream_id: source.capabilities for source in regulation.sources} == {
        "federal-register-digital-assets": ("AGENCY_RULEMAKING",),
        "ofac-recent-actions": ("SANCTIONS_ACTIONS",),
        "treasury-press-releases": ("EXECUTIVE_POLICY_ACTIONS",),
    }
    monetary = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "MONETARY_INFLATION"
    )
    assert tuple(source.stream_id for source in monetary.sources[:2]) == (
        "bea-economic-release-calendar",
        "bls-economic-release-calendar",
    )
    monetary_sources = {source.stream_id: source for source in monetary.sources}
    assert monetary_sources["bea-economic-release-calendar"].capabilities == (
        "OFFICIAL_EVENT_CALENDAR",
    )
    assert monetary_sources["bls-economic-release-calendar"].capabilities == (
        "OFFICIAL_EVENT_CALENDAR",
    )
    fiscal = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "FISCAL_DEBT"
    )
    fiscal_sources = {source.stream_id: source for source in fiscal.sources}
    assert fiscal_sources["treasury-auction-results"].capabilities == (
        "AUCTION_ABSORPTION",
        "DEBT_ISSUANCE",
    )
    assert fiscal_sources["treasury-average-interest-cost"].capabilities == ("DEBT_SERVICE_BURDEN",)
    assert fiscal_sources["treasury-interest-expense"].capabilities == ("DEBT_SERVICE_BURDEN",)
    assert fiscal_sources["treasury-refinancing-profile"].capabilities == ("REFINANCING_PROFILE",)
    cross_asset = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "CROSS_ASSET_EXTERNAL"
    )
    cross_asset_sources = {source.stream_id: source for source in cross_asset.sources}
    assert cross_asset_sources["treasury-real-yield-curve"].capabilities == (
        "UST_REAL_YIELD_CURVE",
    )
    institutional = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "INSTITUTIONAL_FLOWS"
    )
    assert {source.stream_id: source.capabilities for source in institutional.sources} == {
        "ark-arkb-holdings": ("BTC_ETF_ARKB_HOLDINGS",),
        "bitwise-bitb-holdings": ("BTC_ETF_BITB_HOLDINGS",),
        "bykaranteli-etf-aggregate-flows": (
            "BTC_ETF_AGGREGATE_FLOW",
            "ETH_ETF_AGGREGATE_FLOW",
        ),
        "ishares-ibit-holdings": ("BTC_ETF_IBIT_HOLDINGS",),
    }
    spot_derivatives = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "SPOT_DERIVATIVES"
    )
    assert tuple(source.stream_id for source in spot_derivatives.sources) == (
        "binance-usdm-market",
        "coinbase-spot-market",
        "kraken-spot-market",
    )
    assert {source.stream_id: source.capabilities for source in spot_derivatives.sources} == {
        "binance-usdm-market": ("BINANCE_PERPETUAL", "BINANCE_SPOT"),
        "coinbase-spot-market": ("MULTI_VENUE_SPOT",),
        "kraken-spot-market": ("MULTI_VENUE_SPOT",),
    }
    assert spot_derivatives.required_capabilities == (
        "BINANCE_PERPETUAL",
        "BINANCE_SPOT",
        "MULTI_VENUE_SPOT",
        "OPTIONS_POSITIONING",
    )
    onchain = next(
        item
        for item in config.information.coverage_requirements
        if item.domain.value == "ONCHAIN_SUPPLY"
    )
    assert tuple(source.stream_id for source in onchain.sources) == (
        "defillama-usd-stablecoin-supply",
    )
    assert {source.stream_id: source.capabilities for source in onchain.sources} == {
        "defillama-usd-stablecoin-supply": ("STABLECOIN_SUPPLY",),
    }
    assert config.decision_state.packet_policy.maximum_background_fact_distance_seconds == 172_800
    assert config.decision_state.packet_policy.maximum_calendar_context_distance_seconds == 604_800
    assert config.decision_state.official_fact_policy.affected_assets == (
        "BTC",
        "ETH",
        "PAXG",
    )
    assert config.assessment.mandate.required_risk_factors == (
        "US_MONETARY_POLICY",
        "US_INFLATION",
        "US_EMPLOYMENT",
        "US_GROWTH",
        "US_FISCAL_LIQUIDITY",
        "US_FISCAL_CAPACITY",
        "US_REAL_INTEREST_RATES",
        "US_DOLLAR",
        "US_EQUITY_RISK_APPETITE",
        "US_HIGH_YIELD_CREDIT_RISK",
        "CRYPTO_LIQUIDITY_CAPACITY",
        "BTC_INSTITUTIONAL_FLOW",
        "ETH_INSTITUTIONAL_FLOW",
        "US_MONETARY_LIQUIDITY",
        "US_INTEREST_RATES",
        "US_ENERGY_INFLATION",
        "BTC_INSTITUTIONAL_HOLDINGS",
        "EXTERNAL_INFORMATION",
        "MARKET_VOLATILITY",
    )
    assert config.trigger.volatility_jump_threshold == Decimal("0.01")


def test_previous_release_config_defaults_new_forecast_prior_to_disabled() -> None:
    payload = load_config("config/investment-manager.yaml").model_dump(mode="python")
    payload["outcome_evaluation"].pop("forecast_prior")

    loaded = AppConfig.model_validate(payload)

    assert not loaded.outcome_evaluation.forecast_prior.enabled
    assert loaded.outcome_evaluation.forecast_prior.artifact_id is None


def test_world_model_economic_targets_must_equal_capital_universe() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["assessment"]["mandate"]["mandate_exposures"] = payload["assessment"]["mandate"][
        "mandate_exposures"
    ][:-1]

    with pytest.raises(
        ValidationError,
        match="economic exposures 必须精确覆盖 Capital 可投资域",
    ):
        AppConfig.model_validate(payload)


def test_perpetual_quote_cadence_must_satisfy_cross_market_skew() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.market_data.model_dump(mode="python")
    payload["perpetual_quote_poll_seconds"] = (
        config.market_data.maximum_cross_market_quote_skew_seconds + 1
    )

    with pytest.raises(
        ValidationError,
        match="永续报价轮询间隔不得超过跨市场报价偏差上限",
    ):
        MarketDataPolicy.model_validate(payload)


def test_market_observation_domain_may_exceed_assessment_mandate() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["market_data"]["perpetual_instruments"] = (
        *payload["market_data"]["perpetual_instruments"],
        InstrumentId(
            venue="BINANCE",
            product=InstrumentProduct.USD_M_PERPETUAL,
            symbol="XRPUSDT",
            base_asset="XRP",
            quote_asset="USDT",
            settlement_asset="USDT",
        ).model_dump(mode="python"),
    )

    restored = config.__class__.model_validate(payload)

    assert tuple(item.symbol for item in restored.market_data.perpetual_instruments) == (
        "SPYUSDT",
        "XAUUSDT",
        "BTCUSDT",
        "ETHUSDT",
        "PAXGUSDT",
        "XRPUSDT",
    )


@pytest.mark.parametrize(
    ("reference_key", "message"),
    (
        ("BINANCE:TRADFI_PERPETUAL:UNKNOWNUSDT", "必须属于 MarketData 观测域"),
        ("BINANCE:TRADFI_PERPETUAL:SPYUSDT", "只能观察，不能拥有执行权限"),
    ),
)
def test_economic_reference_cannot_escape_observation_boundary(
    reference_key: str,
    message: str,
) -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.model_dump(mode="python")
    payload["assessment"]["mandate"]["observation_references"][0][
        "reference_instrument_key"
    ] = reference_key

    with pytest.raises(ValidationError, match=message):
        AppConfig.model_validate(payload)


def test_shadow_decision_packet_composition_accepts_observation_only_products() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    engine = build_engine("sqlite+pysqlite:///:memory:")
    try:
        assemble_decision_packet_preparation(config, engine)
    finally:
        engine.dispose()


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


def test_previous_official_fact_policy_does_not_require_new_metric_rules() -> None:
    config = load_config("config/investment-manager.yaml")
    payload = config.decision_state.model_dump(mode="python")
    payload["version"] = "portfolio-state-v53"
    payload["official_fact_policy"]["version"] = "official-fact-v20"
    future_fact_types = {
        "US_TREASURY_AVERAGE_INTEREST_COST_SNAPSHOT",
        "US_TREASURY_INTEREST_EXPENSE_SNAPSHOT",
        "US_TREASURY_REFINANCING_PROFILE_SNAPSHOT",
    }
    payload["delta_policy"]["rules"] = tuple(
        item
        for item in payload["delta_policy"]["rules"]
        if item["fact_type"] not in future_fact_types
    )

    restored = DecisionStatePolicy.model_validate(payload)

    assert restored.official_fact_policy.version == "official-fact-v20"


def test_reference_policy_cannot_relabel_the_btc_experiment_as_total_benchmark() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.capital.model_dump(mode="python")
    payload["mandate"]["status"] = MandateStatus.APPROVED
    payload["reference_policy"] = {
        "version": "invalid-btc-reference-v1",
        "mandate_version": config.capital.mandate.version,
        "universe_version": config.capital.investable_universe.version,
        "selection_artifact_id": "invalid-btc-reference-selection-v1",
        "rebalance_band_fraction": Decimal("0.05"),
        "allocations": (
            {
                "implementation_key": "BINANCE:SPOT:BTCUSDT",
                "target_exposure_fraction": Decimal("0.10"),
            },
            {
                "implementation_key": "CASH:USDT",
                "target_exposure_fraction": Decimal("0.90"),
            },
        ),
    }

    with pytest.raises(ValidationError, match="不合格的实现产品"):
        type(config.capital).model_validate(payload)


def test_reference_policy_validation_does_not_use_exposure_count_as_risk_proof() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.capital.model_dump(mode="python")
    payload["mandate"]["status"] = MandateStatus.APPROVED
    payload["investable_universe"]["instruments"][0]["reference_candidate"] = True
    payload["reference_policy"] = {
        "version": "underdiversified-reference-v1",
        "mandate_version": config.capital.mandate.version,
        "universe_version": config.capital.investable_universe.version,
        "selection_artifact_id": "underdiversified-reference-selection-v1",
        "rebalance_band_fraction": Decimal("0.05"),
        "allocations": (
            {
                "implementation_key": "BINANCE:TRADFI_PERPETUAL:SPYUSDT",
                "target_exposure_fraction": Decimal("0.10"),
            },
            {
                "implementation_key": "CASH:USDT",
                "target_exposure_fraction": Decimal("0.90"),
            },
        ),
    }

    validated = type(config.capital).model_validate(payload)
    assert validated.reference_policy is not None
    assert validated.reference_policy.version == "underdiversified-reference-v1"


def test_forecast_reference_products_are_not_capital_products() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    capital = config.capital
    execution_keys = {item.instrument.key for item in capital.execution_specs}
    reference_keys = {item.key for item in capital.forecast_reference_instruments}

    assert execution_keys.isdisjoint(reference_keys)
    assert all(
        item.instrument.product != InstrumentProduct.SPOT for item in capital.execution_specs
    )


def test_provisional_mandate_cannot_grant_reference_policy() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.capital.model_dump(mode="python")
    payload["reference_policy"] = {
        "version": "invalid-provisional-reference-v1",
        "mandate_version": config.capital.mandate.version,
        "universe_version": config.capital.investable_universe.version,
        "selection_artifact_id": "invalid-provisional-reference-selection-v1",
        "rebalance_band_fraction": Decimal("0.05"),
        "allocations": (
            {
                "implementation_key": "BINANCE:SPOT:PAXGUSDT",
                "target_exposure_fraction": Decimal("0.10"),
            },
            {
                "implementation_key": "CASH:USDT",
                "target_exposure_fraction": Decimal("0.90"),
            },
        ),
    }

    with pytest.raises(ValidationError, match="资产所有者已批准"):
        type(config.capital).model_validate(payload)


def test_investable_universe_cannot_exceed_the_owner_mandate() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    payload = config.capital.model_dump(mode="python")
    payload["mandate"]["allowed_exposures"] = tuple(
        exposure
        for exposure in payload["mandate"]["allowed_exposures"]
        if exposure != EconomicExposure.CRYPTO_NETWORK
    )

    with pytest.raises(ValidationError, match="Mandate 未允许"):
        type(config.capital).model_validate(payload)


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
    assert config.market_data.symbols == ("BTCUSDT", "ETHUSDT")
    assert all(
        item.product == InstrumentProduct.USD_M_PERPETUAL
        for item in config.market_data.perpetual_instruments
    )
    assert not config.capital.enabled


def test_config_inheritance_rejects_cycles(tmp_path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("extends: second.yaml\n", encoding="utf-8")
    second.write_text("extends: first.yaml\n", encoding="utf-8")

    with pytest.raises(ValueError, match="存在循环"):
        load_config(first)


def test_ai_input_budgets_cannot_regress_to_unbounded_raw_context() -> None:
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
