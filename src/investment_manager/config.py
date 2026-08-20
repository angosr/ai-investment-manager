from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from investment_manager.domain import EdgeCalibration
from investment_manager.execution.models import SUPPORTED_OPEN_SIDES


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeaturePolicy(StrictConfig):
    version: str
    trend_threshold: Decimal = Decimal("0.003")
    volatility_window: int = Field(default=8, ge=2)


class PanelPolicy(StrictConfig):
    version: str
    schema_version: str = "panel-v1"
    max_characters: int = Field(default=12_000, ge=4_000, le=12_000)
    max_evidence: int = Field(default=20, ge=0, le=100)
    max_per_source: int = Field(default=5, ge=1)
    max_market_age_seconds: int = Field(default=180, ge=1)
    maximum_evidence_age_seconds: int = Field(default=86_400, ge=60, le=604_800)


class StrategyPolicy(StrictConfig):
    strategy_id: str = "price-trend"
    version: str
    family: str = "price-trend"
    enabled: bool = True
    score_threshold: Decimal = Decimal("0.55")
    stop_atr_multiple: Decimal = Decimal("1.5")
    horizon_minutes: int = Field(default=60, gt=0)
    expected_edge_half_life_seconds: int = Field(default=900, ge=1, le=86400)


class CalibrationPolicy(StrictConfig):
    version: str
    minimum_non_overlapping_samples: int = Field(default=30, ge=2)
    method_version: str = "mean-lower-bound-v1"
    lower_confidence_z: Decimal = Field(default=Decimal("1.96"), gt=0)
    artifacts: tuple[EdgeCalibration, ...] = ()

    @model_validator(mode="after")
    def artifacts_must_be_unique_sufficient_and_non_overlapping(self):
        ids = [item.calibration_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("EdgeCalibration calibration_id 必须唯一")
        by_scope: dict[tuple[object, ...], list[EdgeCalibration]] = {}
        for artifact in self.artifacts:
            if artifact.non_overlapping_sample_size < self.minimum_non_overlapping_samples:
                raise ValueError("发布校准制品的非重叠样本量不足")
            if artifact.method_version != self.method_version:
                raise ValueError("EdgeCalibration 构建方法版本与策略不一致")
            by_scope.setdefault(artifact.scope, []).append(artifact)
        for scoped in by_scope.values():
            ordered = sorted(scoped, key=lambda item: item.valid_from)
            if any(
                current.valid_from < previous.valid_until for previous, current in pairwise(ordered)
            ):
                raise ValueError("同一校准作用域的有效期不得重叠")
        return self


class CompositionPolicy(StrictConfig):
    version: str


class FrequencyPolicy(StrictConfig):
    version: str
    cooldown_minutes: int = Field(default=30, ge=0)
    maximum_orders_per_day: int = Field(default=8, gt=0)
    minimum_net_edge_bps: Decimal = Field(default=Decimal("4"), ge=0)
    funding_bps: Decimal = Decimal("0")
    latency_bps: Decimal = Field(default=Decimal("0.5"), ge=0)
    adverse_selection_bps: Decimal = Field(default=Decimal("0.75"), ge=0)
    uncertainty_buffer_bps: Decimal = Field(default=Decimal("1.5"), ge=0)


class RiskPolicy(StrictConfig):
    version: str
    symbol_allowlist: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    maximum_market_age_seconds: int = Field(default=180, gt=0)
    maximum_account_age_seconds: int = Field(default=60, gt=0)
    maximum_risk_fraction: Decimal = Decimal("0.005")
    maximum_total_risk_fraction: Decimal = Decimal("0.02")
    maximum_position_notional: Decimal = Decimal("2000")
    maximum_daily_loss: Decimal = Decimal("200")
    maximum_drawdown_fraction: Decimal = Decimal("0.05")
    minimum_order_notional: Decimal = Decimal("10")
    quantity_step: Decimal = Decimal("0.000001")
    reservation_ttl_seconds: int = Field(default=120, gt=0)
    kill_switch: bool = False


class ExecutionPolicy(StrictConfig):
    version: str
    fee_bps: Decimal = Field(default=Decimal("10"), ge=0)
    market_slippage_bps: Decimal = Field(default=Decimal("2"), ge=0)
    default_fill_fraction: Decimal = Field(default=Decimal("1"), gt=0, le=1)


class ReconciliationPolicy(StrictConfig):
    version: str
    poll_seconds: int = Field(default=60, ge=5, le=3600)
    maximum_report_age_seconds: int = Field(default=180, ge=10, le=3600)
    balance_tolerance: Decimal = Field(default=Decimal("0.00000001"), ge=0)
    position_quantity_tolerance: Decimal = Field(default=Decimal("0.00000001"), ge=0)


class OutcomeEvaluationPolicy(StrictConfig):
    version: str
    forecast_version: str = "analysis-forecast-v3"
    window_hours: int = Field(default=24, ge=1, le=168)
    settlement_grace_minutes: int = Field(default=120, ge=0, le=1440)
    poll_seconds: int = Field(default=300, ge=10, le=3600)


class GovernancePolicy(StrictConfig):
    version: str
    snapshot_lookback_days: int = Field(default=90, ge=7, le=3650)
    maximum_metric_windows: int = Field(default=30, ge=1, le=200)
    maximum_failed_experiments: int = Field(default=20, ge=1, le=100)
    maximum_open_proposals: int = Field(default=20, ge=1, le=100)
    maximum_evaluation_plans: int = Field(default=10, ge=1, le=50)
    complexity_limit: int = Field(default=10, ge=0, le=100)
    cycle_interval_hours: int = Field(default=24, ge=1, le=168)


class TriggerPolicy(StrictConfig):
    version: str
    heartbeat_minutes: int = Field(default=15, ge=1, le=240)
    debounce_seconds: int = Field(default=120, ge=0, le=3600)
    high_impact_threshold: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    volatility_jump_threshold: Decimal = Field(default=Decimal("0.02"), gt=0)
    volatility_window_seconds: int = Field(default=600, ge=60, le=86_400)
    minimum_call_interval_seconds: int = Field(default=15, ge=1, le=3600)
    maximum_scheduled_wakeups: int = Field(default=64, ge=1, le=500)
    maximum_event_rules: int = Field(default=32, ge=1, le=200)
    maximum_batch_size: int = Field(default=100, ge=1, le=1000)
    maximum_pending_triggers: int = Field(default=1000, ge=1, le=10000)
    trigger_expiry_seconds: int = Field(default=900, ge=30, le=86400)
    outbox_fallback_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    dispatcher_advisory_lock_key: int = Field(default=817_221_901, ge=1)


class TemporalPolicy(StrictConfig):
    """持久化编排参数；每次启动 Workflow 时会冻结为输入的一部分。"""

    version: str
    address: str = "127.0.0.1:7233"
    namespace: str = "default"
    task_queue: str = "quant-core-analysis-v1"
    lifecycle_task_queue: str = "quant-core-lifecycle-v1"
    reconciliation_task_queue: str = "quant-core-reconciliation-v1"
    outcome_evaluation_task_queue: str = "quant-core-outcome-evaluation-v1"
    governance_task_queue: str = "quant-core-governance-v1"
    version_evaluation_task_queue: str = "quant-core-version-evaluation-v1"
    release_task_queue: str = "quant-core-release-v1"
    trigger_task_queue: str = "quant-core-trigger-v1"
    activity_start_to_close_seconds: int = Field(default=240, ge=10, le=900)
    activity_schedule_to_close_seconds: int = Field(default=600, ge=10, le=1800)
    retry_initial_seconds: int = Field(default=2, ge=1, le=60)
    retry_maximum_seconds: int = Field(default=30, ge=1, le=300)
    retry_backoff_coefficient: float = Field(default=2.0, ge=1, le=10)
    retry_maximum_attempts: int = Field(default=3, ge=1, le=10)
    worker_threads: int = Field(default=4, ge=1, le=32)

    @model_validator(mode="after")
    def temporal_bounds_must_be_consistent(self):
        if self.activity_schedule_to_close_seconds < self.activity_start_to_close_seconds:
            raise ValueError("Temporal schedule-to-close 不得短于 start-to-close")
        if self.retry_maximum_seconds < self.retry_initial_seconds:
            raise ValueError("Temporal 最大重试间隔不得短于初始间隔")
        if not all(
            item.strip()
            for item in (
                self.address,
                self.namespace,
                self.task_queue,
                self.lifecycle_task_queue,
                self.reconciliation_task_queue,
                self.outcome_evaluation_task_queue,
                self.governance_task_queue,
                self.version_evaluation_task_queue,
                self.release_task_queue,
                self.trigger_task_queue,
            )
        ):
            raise ValueError("Temporal address、namespace、task_queue 不得为空")
        return self


class MarketDataPolicy(StrictConfig):
    version: str
    symbols: tuple[str, ...] = Field(default=("BTCUSDT", "ETHUSDT"), min_length=1, max_length=20)
    interval: str = Field(
        default="5m", pattern=r"^(1m|3m|5m|15m|30m|1h|2h|4h|1d)$"
    )
    bar_window: int = Field(default=64, ge=8, le=1000)
    rest_base_url: str = "https://api.binance.com"
    websocket_base_url: str = "wss://stream.binance.com:9443"
    rest_timeout_seconds: int = Field(default=10, ge=1, le=30)
    stream_silence_seconds: int = Field(default=60, ge=10, le=300)
    planned_reconnect_seconds: int = Field(default=85_800, ge=3600, le=86_300)
    reconnect_initial_seconds: int = Field(default=1, ge=1, le=30)
    reconnect_maximum_seconds: int = Field(default=30, ge=1, le=300)
    quote_persist_interval_ms: int = Field(default=1000, ge=100, le=60_000)
    trade_persist_interval_ms: int = Field(default=1000, ge=100, le=60_000)

    @property
    def interval_seconds(self) -> int:
        unit_seconds = {"m": 60, "h": 3600, "d": 86_400}
        return int(self.interval[:-1]) * unit_seconds[self.interval[-1]]

    @field_validator("symbols")
    @classmethod
    def symbols_must_be_canonical(cls, symbols: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(symbols)) != len(symbols):
            raise ValueError("行情 symbols 不得重复")
        if any(not symbol.isalnum() or symbol != symbol.upper() for symbol in symbols):
            raise ValueError("行情 symbol 必须是大写字母数字")
        return symbols

    @model_validator(mode="after")
    def public_endpoints_and_backoff_must_be_safe(self):
        endpoint_pairs = {
            (
                "https://api.binance.com",
                "wss://stream.binance.com:9443",
            ),
            (
                "https://testnet.binance.vision",
                "wss://stream.testnet.binance.vision",
            ),
        }
        if (self.rest_base_url, self.websocket_base_url) not in endpoint_pairs:
            raise ValueError("行情 REST 与 WebSocket 必须使用同一 Binance 官方环境")
        if self.reconnect_maximum_seconds < self.reconnect_initial_seconds:
            raise ValueError("行情最大重连间隔不得短于初始间隔")
        return self


class ShadowSimulationPolicy(StrictConfig):
    version: str
    initial_quote_balance: Decimal = Field(default=Decimal("10000"), gt=0)
    analysis_deadline_seconds: int = Field(default=300, ge=30, le=900)
    lifecycle_poll_seconds: int = Field(default=10, ge=2, le=60)


class InformationPolicy(StrictConfig):
    version: str
    trendradar_mcp_url: str = "http://127.0.0.1:3333/mcp"
    newsnow_base_url: str = "http://127.0.0.1:4444"
    newsnow_sources: tuple[str, ...] = ()
    source_timezone: str = "Asia/Shanghai"
    platforms: tuple[str, ...] = ()
    read_limit: int = Field(default=100, ge=1, le=1000)
    request_timeout_seconds: int = Field(default=15, ge=1, le=60)
    collection_interval_seconds: int = Field(default=60, ge=10, le=600)

    @field_validator("trendradar_mcp_url")
    @classmethod
    def mcp_must_be_loopback(cls, value: str) -> str:
        if not value.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("信息 MCP 必须是显式回环地址")
        return value

    @field_validator("newsnow_base_url")
    @classmethod
    def newsnow_must_be_loopback(cls, value: str) -> str:
        if not value.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("NewsNow 必须是显式回环地址")
        return value.rstrip("/")

    @field_validator("newsnow_sources")
    @classmethod
    def newsnow_sources_must_be_unique_and_safe(cls, sources: tuple[str, ...]) -> tuple[str, ...]:
        if len(sources) != len(set(sources)):
            raise ValueError("NewsNow source id 不得重复")
        if any(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item) is None for item in sources):
            raise ValueError("NewsNow source id 非法")
        return sources


class AiMode(StrEnum):
    OFF = "OFF"
    PROPOSE = "PROPOSE"


class DeploymentStage(StrEnum):
    MOCK = "MOCK"
    SHADOW = "SHADOW"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class PipelinePolicy(StrictConfig):
    version: str
    ai_mode: AiMode = AiMode.OFF


class ProposalPolicy(StrictConfig):
    version: str
    producer_id: str = "codex-analyst"
    strategy_family: str = "ai-contextual"
    minimum_confidence: Decimal = Decimal("0.55")
    maximum_horizon_minutes: int = Field(default=240, gt=0)
    forecast_horizons_minutes: tuple[int, ...] = Field(
        default=(60, 240), min_length=1, max_length=4
    )
    expected_edge_half_life_seconds: int = Field(default=1800, ge=1, le=86400)

    @model_validator(mode="after")
    def forecast_horizons_are_unique_ordered_and_bounded(self):
        horizons = self.forecast_horizons_minutes
        if tuple(sorted(set(horizons))) != horizons:
            raise ValueError("方向预测周期必须正数、唯一且升序")
        if horizons[0] <= 0 or horizons[-1] > self.maximum_horizon_minutes:
            raise ValueError("方向预测周期必须在 Proposal 最大周期内")
        return self


class CodexAccount(StrictConfig):
    account_id: str = Field(pattern=r"^\.?[a-z][a-z0-9._-]{1,63}$")
    codex_home: Path
    enabled: bool = False
    capacity_weight: Decimal = Field(default=Decimal("1"), gt=0)

    @field_validator("codex_home")
    @classmethod
    def codex_home_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("codex_home 必须是绝对路径")
        return value

    @model_validator(mode="after")
    def account_id_must_match_directory_name(self):
        if self.account_id != self.codex_home.name:
            raise ValueError("Codex account_id 必须与 codex_home 目录名一致")
        return self


class CodexAccountRegistry(StrictConfig):
    version: str
    accounts: tuple[CodexAccount, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def aliases_and_paths_must_be_unique(self):
        aliases = [item.account_id for item in self.accounts]
        paths = [item.codex_home for item in self.accounts]
        if len(set(aliases)) != len(aliases):
            raise ValueError("Codex account_id 必须唯一")
        if len(set(paths)) != len(paths):
            raise ValueError("Codex codex_home 必须唯一")
        return self


class CodexRuntimePolicy(StrictConfig):
    version: str
    enabled: bool = False
    isolation_verified: bool = False
    binary: Path = Path("/usr/bin/codex")
    bundle_root: Path = Path("/var/lib/quant-core/codex-runs")
    expected_cli_version: str
    expected_binary_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    model: str
    reasoning_effort: str = Field(pattern=r"^(low|medium|high|xhigh|max|ultra)$")
    maximum_prompt_characters: int = Field(default=16_000, ge=8_000, le=16_000)
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    capacity_probe_timeout_seconds: int = Field(default=10, ge=1, le=60)
    capacity_ttl_seconds: int = Field(default=60, ge=1, le=60)
    transient_failure_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    lease_ttl_seconds: int = Field(default=240, ge=30, le=1200)
    max_account_switches: int = Field(default=2, ge=0, le=2)

    @field_validator("binary")
    @classmethod
    def binary_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Codex binary 必须是绝对路径")
        return value

    @field_validator("bundle_root")
    @classmethod
    def bundle_root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Codex bundle_root 必须是绝对路径")
        return value

    @model_validator(mode="after")
    def production_requires_verified_isolation(self):
        if self.enabled and not self.isolation_verified:
            raise ValueError("启用真实 Codex 前必须完成 OS/Profile 隔离验证")
        if self.enabled and self.expected_binary_sha256 is None:
            raise ValueError("启用真实 Codex 前必须冻结可执行文件 SHA-256")
        return self


class BinanceTestnetPolicy(StrictConfig):
    version: str
    rest_base_url: str = "https://testnet.binance.vision/api"
    recv_window_ms: int = Field(default=5000, ge=1000, le=5000)
    request_timeout_seconds: int = Field(default=10, ge=1, le=10)
    time_sync_ttl_seconds: int = Field(default=30, ge=1, le=300)
    quote_asset: str = Field(default="USDT", pattern=r"^[A-Z0-9]{2,16}$")
    credential_environment_prefix: str = "QUANT_CORE_BINANCE"

    @model_validator(mode="after")
    def testnet_endpoint_and_credentials_must_be_fixed(self):
        if self.rest_base_url != "https://testnet.binance.vision/api":
            raise ValueError("Binance Testnet 只允许官方 REST 端点")
        if self.credential_environment_prefix != "QUANT_CORE_BINANCE":
            raise ValueError("Binance Testnet 凭证环境变量前缀不可变更")
        return self


class DeploymentPolicy(StrictConfig):
    version: str
    stage: DeploymentStage = DeploymentStage.MOCK
    shadow_market_data_enabled: bool = False
    testnet_order_submission_enabled: bool = False
    live_order_submission_enabled: bool = False
    credential_profile: str | None = None
    manual_approval_ref: str | None = None

    @model_validator(mode="after")
    def stage_permissions_are_fail_closed(self):
        if self.stage == DeploymentStage.MOCK and any(
            (
                self.shadow_market_data_enabled,
                self.testnet_order_submission_enabled,
                self.live_order_submission_enabled,
                self.credential_profile is not None,
            )
        ):
            raise ValueError("MOCK 阶段不得启用外部数据凭据或订单权限")
        if self.stage == DeploymentStage.SHADOW and (
            not self.shadow_market_data_enabled
            or self.testnet_order_submission_enabled
            or self.live_order_submission_enabled
        ):
            raise ValueError("SHADOW 只允许实时只读行情，不允许任何订单提交")
        if self.stage == DeploymentStage.TESTNET and (
            not self.shadow_market_data_enabled
            or not self.testnet_order_submission_enabled
            or self.live_order_submission_enabled
            or self.credential_profile is None
            or self.manual_approval_ref is None
        ):
            raise ValueError("TESTNET 必须显式启用测试网、凭据 Profile 和人工批准")
        if self.stage == DeploymentStage.LIVE:
            raise ValueError("LIVE 适配器未实现，配置层禁止启用")
        return self


class AppConfig(StrictConfig):
    feature: FeaturePolicy
    panel: PanelPolicy
    strategy: StrategyPolicy
    calibration: CalibrationPolicy
    composition: CompositionPolicy
    frequency: FrequencyPolicy
    risk: RiskPolicy
    execution: ExecutionPolicy
    reconciliation: ReconciliationPolicy
    outcome_evaluation: OutcomeEvaluationPolicy
    governance: GovernancePolicy
    trigger: TriggerPolicy
    temporal: TemporalPolicy
    market_data: MarketDataPolicy
    shadow: ShadowSimulationPolicy
    information: InformationPolicy
    pipeline: PipelinePolicy
    proposal: ProposalPolicy
    codex_runtime: CodexRuntimePolicy
    codex_accounts: CodexAccountRegistry
    binance_testnet: BinanceTestnetPolicy
    deployment: DeploymentPolicy

    @model_validator(mode="after")
    def market_symbols_must_be_risk_allowed(self):
        if not set(self.market_data.symbols).issubset(self.risk.symbol_allowlist):
            raise ValueError("行情 symbols 必须是风控允许品种的子集")
        if any(
            not symbol.endswith(self.binance_testnet.quote_asset)
            for symbol in self.market_data.symbols
        ):
            raise ValueError("行情 symbols 必须使用配置的统一 quote_asset")
        testnet_market_data = self.market_data.rest_base_url == "https://testnet.binance.vision"
        if self.deployment.stage == DeploymentStage.TESTNET and not testnet_market_data:
            raise ValueError("TESTNET 必须使用 Binance Spot Testnet 行情")
        if self.deployment.stage != DeploymentStage.TESTNET and testnet_market_data:
            raise ValueError("非 TESTNET 阶段不得混用 Binance Spot Testnet 行情")
        if self.pipeline.ai_mode == AiMode.PROPOSE:
            enabled_accounts = sum(item.enabled for item in self.codex_accounts.accounts)
            if self.temporal.worker_threads > enabled_accounts:
                raise ValueError("PROPOSE 分析并发不得超过已启用 Codex 账号数")
        active_producers = {
            (self.strategy.strategy_id, self.strategy.version): self.strategy.horizon_minutes,
        }
        if self.pipeline.ai_mode == AiMode.PROPOSE:
            active_producers[(self.proposal.producer_id, self.proposal.version)] = None
        for artifact in self.calibration.artifacts:
            producer = (artifact.producer_id, artifact.producer_version)
            if producer not in active_producers:
                raise ValueError("校准制品必须绑定当前管线实际装配的 Producer 版本")
            expected_horizon = active_producers[producer]
            if expected_horizon is not None and artifact.horizon_minutes != expected_horizon:
                raise ValueError("程序策略校准周期必须与当前策略周期一致")
            if (
                expected_horizon is None
                and artifact.horizon_minutes > self.proposal.maximum_horizon_minutes
            ):
                raise ValueError("AI 校准周期超过 ProposalPolicy 上限")
            if artifact.symbol not in self.market_data.symbols:
                raise ValueError("校准制品品种必须属于当前行情 universe")
            if artifact.side not in SUPPORTED_OPEN_SIDES:
                raise ValueError("校准制品方向必须属于当前允许建仓方向")
        return self


def load_config(path: str | Path) -> AppConfig:
    """加载严格配置；小型环境文件可用 ``extends`` 继承同目录基线。"""

    config_path = Path(path).resolve()
    return AppConfig.model_validate(_load_config_mapping(config_path, stack=()))


def _load_config_mapping(config_path: Path, *, stack: tuple[Path, ...]) -> dict[str, Any]:
    if config_path in stack:
        chain = " -> ".join(str(item) for item in (*stack, config_path))
        raise ValueError(f"配置 extends 存在循环: {chain}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是对象")
    current = dict(raw)
    base_reference = current.pop("extends", None)
    if base_reference is None:
        return current
    if not isinstance(base_reference, str) or not base_reference.strip():
        raise ValueError("配置 extends 必须是非空路径")
    base_path = Path(base_reference)
    if not base_path.is_absolute():
        base_path = config_path.parent / base_path
    base = _load_config_mapping(base_path.resolve(), stack=(*stack, config_path))
    return _merge_config(base, current)


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_config(existing, value)
        else:
            merged[key] = value
    return merged
