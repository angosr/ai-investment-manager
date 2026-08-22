"""Point-in-time diagnostic replay for the active rolling carry hypothesis.

The frozen carry bundle currently has daily prices but exact funding settlement
times.  This module therefore answers a narrow question: would the production
signal and its hysteresis have survived fees at UTC daily opens?  It is useful
for rejecting weak economics, but cannot authorize a strategy whose live clock
runs every 15 minutes.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, floor_to_step
from investment_manager.market.models import ClosedMarketBar, InstrumentProduct
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.carry import (
    CarryFundingSettlement,
    CarryMarketDay,
    HistoricalCarryDataset,
)
from investment_manager.research.dataset import HistoricalDataset
from investment_manager.settings import AppConfig

_BPS = Decimal("10000")


class DynamicCarryReplayPolicy(FrozenModel):
    """Minimal immutable projection of every production rule used by replay."""

    version: Literal["dynamic-carry-daily-open-diagnostic-v1"] = (
        "dynamic-carry-daily-open-diagnostic-v1"
    )
    symbol: str = Field(min_length=1)
    forecast_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capital_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    funding_lookback_hours: int = Field(gt=0)
    minimum_funding_settlements: int = Field(gt=0)
    forecast_horizon_hours: int = Field(gt=0)
    funding_interval_hours: int = Field(gt=0)
    estimated_round_trip_cost_bps: Decimal = Field(ge=0)
    minimum_entry_net_bps: Decimal
    minimum_hold_net_bps: Decimal
    gross_allocation_fraction: Decimal = Field(gt=0, le=1)
    minimum_rebalance_notional: Decimal = Field(ge=0)
    common_quantity_step: Decimal = Field(gt=0)
    spot_minimum_notional: Decimal = Field(ge=0)
    perpetual_minimum_notional: Decimal = Field(ge=0)
    spot_fee_bps: Decimal = Field(ge=0)
    perpetual_fee_bps: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def thresholds_and_costs_match(self):
        if self.minimum_hold_net_bps > self.minimum_entry_net_bps:
            raise ValueError("Dynamic Carry 持有门槛不能高于入场门槛")
        modeled_round_trip = self.spot_fee_bps + self.perpetual_fee_bps
        if modeled_round_trip != self.estimated_round_trip_cost_bps:
            raise ValueError("Dynamic Carry 信号成本必须等于双腿完整往返费用")
        return self


class DynamicCarryReplayAction(FrozenModel):
    at: datetime
    kind: Literal["ENTRY", "REBALANCE", "SIGNAL_EXIT", "BOUNDARY_EXIT"]
    gross_signal_bps: Decimal | None = None
    net_signal_bps: Decimal | None = None
    prior_quantity: Decimal = Field(ge=0)
    target_quantity: Decimal = Field(ge=0)
    modeled_cost: Decimal = Field(ge=0)

    _utc_at = field_validator("at")(require_utc)


class DynamicCarryReplayMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal
    net_pnl: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    return_fraction: Decimal
    simple_annualized_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    day_count: int = Field(gt=0)
    signal_day_count: int = Field(ge=0)
    missing_signal_day_count: int = Field(ge=0)
    entry_eligible_day_count: int = Field(ge=0)
    exposure_day_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    rebalance_count: int = Field(ge=0)
    signal_exit_count: int = Field(ge=0)
    boundary_exit_count: int = Field(ge=0, le=1)
    maximum_gross_signal_bps: Decimal | None = None
    latest_gross_signal_bps: Decimal | None = None

    @model_validator(mode="after")
    def metrics_reconcile(self):
        if self.ending_equity - self.starting_equity != self.net_pnl:
            raise ValueError("Dynamic Carry 权益与净损益不一致")
        if self.funding_pnl + self.basis_pnl - self.modeled_cost != self.net_pnl:
            raise ValueError("Dynamic Carry 资金费、基差和成本无法核对")
        if self.signal_day_count + self.missing_signal_day_count != self.day_count:
            raise ValueError("Dynamic Carry 信号日数量无法核对")
        return self


class DynamicCarryReplayResult(FrozenModel):
    version: Literal["dynamic-carry-daily-open-replay-v1"] = "dynamic-carry-daily-open-replay-v1"
    result_id: str
    evidence_scope: Literal["REJECTION_ONLY_DIAGNOSTIC"] = "REJECTION_ONLY_DIAGNOSTIC"
    carry_dataset_id: str
    spot_dataset_id: str
    funding_dataset_id: str
    policy: DynamicCarryReplayPolicy
    start: datetime
    end: datetime
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    actions: tuple[DynamicCarryReplayAction, ...]
    metrics: DynamicCarryReplayMetrics

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)

    @model_validator(mode="after")
    def identity_matches(self):
        payload = self.model_dump(exclude={"result_id"})
        expected = stable_id("dynamic_carry_replay", content_hash(payload))
        if self.result_id != expected:
            raise ValueError("Dynamic Carry 诊断结果 ID 与内容不一致")
        return self


class DynamicCarryReplayEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: DynamicCarryReplayResult


class DynamicCarryReplayCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: DynamicCarryReplayResult) -> Path:
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一 Dynamic Carry 诊断 ID 的内容不一致")
            return target
        envelope = DynamicCarryReplayEnvelope(
            result_hash=content_hash(result),
            result=result,
        )
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".dynamic-carry-replay-",
            payload=envelope,
        )

    def load(self, result_id: str) -> DynamicCarryReplayResult:
        raw = json.loads((self._root / f"{result_id}.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(raw.get("result")):
            raise ValueError("Dynamic Carry 诊断制品内容哈希不匹配")
        envelope = DynamicCarryReplayEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("Dynamic Carry 诊断文件名与内容 ID 不一致")
        return envelope.result


def replay_policy_from_config(config: AppConfig) -> DynamicCarryReplayPolicy:
    """Freeze the exact active signal, permission, sizing and execution costs."""

    if not config.capital.enabled or not config.dynamic_carry_forecast.enabled:
        raise ValueError("Dynamic Carry 诊断要求启用当前 Shadow Capital 主动链")
    if config.carry_forecast.evidence is None:
        raise ValueError("Dynamic Carry 诊断缺少 Capital 使用的成本证据")
    if len(config.capital.mock_candidate_authorizations) != 1:
        raise ValueError("Dynamic Carry 诊断要求唯一 Mock candidate authorization")
    permission = config.capital.mock_candidate_authorizations[0]
    execution_specs = config.capital.execution_specs
    specs = {item.instrument.product: item for item in execution_specs}
    if len(execution_specs) != 2 or set(specs) != {
        InstrumentProduct.SPOT,
        InstrumentProduct.USD_M_PERPETUAL,
    }:
        raise ValueError("Dynamic Carry 诊断要求唯一 Spot/Perpetual 执行规格")
    spot = specs[InstrumentProduct.SPOT]
    perpetual = specs[InstrumentProduct.USD_M_PERPETUAL]
    common_step = max(spot.quantity_step, perpetual.quantity_step)
    if any(common_step % item.quantity_step != 0 for item in (spot, perpetual)):
        raise ValueError("Dynamic Carry 双腿数量步长不存在共同可执行倍数")

    risk = config.capital.risk
    sleeve_risk = config.capital.sleeve_risk
    stress_fraction = (
        sleeve_risk.basis_stress_bps
        + sleeve_risk.funding_stress_bps
        + sleeve_risk.execution_stress_bps
    ) / _BPS
    margin_fraction = (
        Decimal("0.5") + Decimal("0.5") * sleeve_risk.derivative_initial_margin_fraction
    )
    allocation_limits = [
        permission.maximum_allocation_fraction,
        config.capital.decision.maximum_total_exposure_fraction,
        config.capital.decision.maximum_single_sleeve_fraction,
        risk.maximum_gross_exposure_fraction,
        risk.maximum_instrument_fraction * 2,
        risk.maximum_margin_fraction / margin_fraction,
    ]
    if stress_fraction > 0:
        allocation_limits.append(risk.maximum_stress_loss_fraction / stress_fraction)
    return DynamicCarryReplayPolicy(
        symbol=config.dynamic_carry_forecast.symbol,
        forecast_policy_hash=content_hash(config.dynamic_carry_forecast),
        authorization_hash=content_hash(permission),
        capital_policy_hash=content_hash(config.capital),
        funding_lookback_hours=(config.dynamic_carry_forecast.funding_lookback_hours),
        minimum_funding_settlements=(config.dynamic_carry_forecast.minimum_funding_settlements),
        forecast_horizon_hours=(config.dynamic_carry_forecast.forecast_horizon_hours),
        funding_interval_hours=(config.dynamic_carry_forecast.funding_interval_hours),
        estimated_round_trip_cost_bps=(config.carry_forecast.evidence.round_trip_cost_bps),
        minimum_entry_net_bps=permission.minimum_entry_net_bps,
        minimum_hold_net_bps=permission.minimum_hold_net_bps,
        gross_allocation_fraction=min(allocation_limits),
        minimum_rebalance_notional=(config.capital.decision.minimum_rebalance_notional),
        common_quantity_step=common_step,
        spot_minimum_notional=spot.minimum_order_notional,
        perpetual_minimum_notional=perpetual.minimum_order_notional,
        spot_fee_bps=spot.fee_bps,
        perpetual_fee_bps=perpetual.fee_bps,
    )


def run_dynamic_carry_replay(
    *,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    policy: DynamicCarryReplayPolicy,
    starting_equity: Decimal,
    start: datetime,
    end: datetime,
) -> DynamicCarryReplayResult:
    """Replay daily-open production decisions with settlement-time visibility."""

    start = require_utc(start)
    end = require_utc(end)
    if starting_equity <= 0:
        raise ValueError("Dynamic Carry 诊断初始权益必须为正数")
    if start >= end:
        raise ValueError("Dynamic Carry 诊断起点必须早于终点")
    days, spot_by_open = _validated_window(
        carry_dataset=carry_dataset,
        spot_dataset=spot_dataset,
        policy=policy,
        start=start,
        end=end,
    )
    settlements = carry_dataset.settlements
    settlement_times = tuple(item.funding_time for item in settlements)
    equity = starting_equity
    peak = equity
    maximum_drawdown = Decimal("0")
    quantity = Decimal("0")
    prior_open_at: datetime | None = None
    prior_spot_open: Decimal | None = None
    prior_contract_open: Decimal | None = None
    funding_pnl = Decimal("0")
    basis_pnl = Decimal("0")
    modeled_cost = Decimal("0")
    actions: list[DynamicCarryReplayAction] = []
    signals: list[Decimal] = []
    missing_signals = 0
    entry_eligible_days = 0
    exposure_days = 0
    entry_count = 0
    rebalance_count = 0
    signal_exit_count = 0

    for day in days:
        spot = spot_by_open[day.open_time]
        if prior_open_at is not None:
            assert prior_spot_open is not None and prior_contract_open is not None
            basis_change = quantity * (
                (spot.open - prior_spot_open) - (day.contract_open - prior_contract_open)
            )
            funding_change = quantity * _settlement_value_between(
                settlements=settlements,
                settlement_times=settlement_times,
                start=prior_open_at,
                end=day.open_time,
            )
            equity += basis_change + funding_change
            basis_pnl += basis_change
            funding_pnl += funding_change
            peak = max(peak, equity)
            maximum_drawdown = max(
                maximum_drawdown,
                Decimal("1") - equity / peak,
            )

        signal = _daily_open_signal(
            carry_dataset=carry_dataset,
            settlement_times=settlement_times,
            policy=policy,
            at=day.open_time,
            spot_open=spot.open,
            contract_open=day.contract_open,
        )
        current_gross = quantity * (spot.open + day.contract_open)
        target_quantity = quantity
        if signal is None:
            missing_signals += 1
        else:
            signals.append(signal)
            net_signal = signal - policy.estimated_round_trip_cost_bps
            if net_signal >= policy.minimum_entry_net_bps:
                entry_eligible_days += 1
            selected = net_signal >= (
                policy.minimum_hold_net_bps if quantity > 0 else policy.minimum_entry_net_bps
            )
            desired_gross = equity * policy.gross_allocation_fraction if selected else Decimal("0")
            turnover = abs(desired_gross - current_gross)
            if Decimal("0") < turnover < policy.minimum_rebalance_notional:
                desired_gross = current_gross
            target_quantity = floor_to_step(
                desired_gross / (spot.open + day.contract_open),
                policy.common_quantity_step,
            )
            if target_quantity != quantity and not _group_is_executable(
                prior_quantity=quantity,
                target_quantity=target_quantity,
                spot_price=spot.open,
                perpetual_price=day.contract_open,
                policy=policy,
            ):
                target_quantity = quantity
            if target_quantity != quantity:
                prior_quantity = quantity
                cost = _execution_cost(
                    quantity_delta=abs(target_quantity - quantity),
                    spot_price=spot.open,
                    perpetual_price=day.contract_open,
                    policy=policy,
                )
                equity -= cost
                modeled_cost += cost
                kind: Literal["ENTRY", "REBALANCE", "SIGNAL_EXIT", "BOUNDARY_EXIT"]
                if prior_quantity == 0:
                    kind = "ENTRY"
                    entry_count += 1
                elif target_quantity == 0:
                    kind = "SIGNAL_EXIT"
                    signal_exit_count += 1
                else:
                    kind = "REBALANCE"
                    rebalance_count += 1
                actions.append(
                    DynamicCarryReplayAction(
                        at=day.open_time,
                        kind=kind,
                        gross_signal_bps=signal,
                        net_signal_bps=net_signal,
                        prior_quantity=prior_quantity,
                        target_quantity=target_quantity,
                        modeled_cost=cost,
                    )
                )
                quantity = target_quantity
                peak = max(peak, equity)
                maximum_drawdown = max(
                    maximum_drawdown,
                    Decimal("1") - equity / peak,
                )

        if quantity > 0:
            exposure_days += 1
            conservative_intraday_equity = equity + quantity * (
                (spot.low - spot.open) - (day.contract_high - day.contract_open)
            )
            maximum_drawdown = max(
                maximum_drawdown,
                Decimal("1") - conservative_intraday_equity / peak,
            )
        prior_open_at = day.open_time
        prior_spot_open = spot.open
        prior_contract_open = day.contract_open

    last_day = days[-1]
    last_spot = spot_by_open[last_day.open_time]
    basis_change = quantity * (
        (last_spot.close - last_spot.open) - (last_day.contract_close - last_day.contract_open)
    )
    funding_change = quantity * _settlement_value_between(
        settlements=settlements,
        settlement_times=settlement_times,
        start=last_day.open_time,
        end=last_day.close_time,
    )
    equity += basis_change + funding_change
    basis_pnl += basis_change
    funding_pnl += funding_change
    peak = max(peak, equity)
    maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)
    boundary_exit_count = 0
    if quantity > 0:
        closing_cost = _execution_cost(
            quantity_delta=quantity,
            spot_price=last_spot.close,
            perpetual_price=last_day.contract_close,
            policy=policy,
        )
        equity -= closing_cost
        modeled_cost += closing_cost
        actions.append(
            DynamicCarryReplayAction(
                at=last_day.close_time,
                kind="BOUNDARY_EXIT",
                gross_signal_bps=signals[-1] if signals else None,
                net_signal_bps=(
                    signals[-1] - policy.estimated_round_trip_cost_bps if signals else None
                ),
                prior_quantity=quantity,
                target_quantity=Decimal("0"),
                modeled_cost=closing_cost,
            )
        )
        boundary_exit_count = 1
        maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)

    elapsed_days = Decimal(
        str((days[-1].close_time - days[0].open_time).total_seconds())
    ) / Decimal("86400")
    net_pnl = equity - starting_equity
    metrics = DynamicCarryReplayMetrics(
        starting_equity=starting_equity,
        ending_equity=equity,
        net_pnl=net_pnl,
        funding_pnl=funding_pnl,
        basis_pnl=basis_pnl,
        modeled_cost=modeled_cost,
        return_fraction=net_pnl / starting_equity,
        simple_annualized_return_fraction=(
            net_pnl / starting_equity * Decimal("365.25") / elapsed_days
        ),
        maximum_drawdown_fraction=maximum_drawdown,
        day_count=len(days),
        signal_day_count=len(signals),
        missing_signal_day_count=missing_signals,
        entry_eligible_day_count=entry_eligible_days,
        exposure_day_count=exposure_days,
        entry_count=entry_count,
        rebalance_count=rebalance_count,
        signal_exit_count=signal_exit_count,
        boundary_exit_count=boundary_exit_count,
        maximum_gross_signal_bps=max(signals) if signals else None,
        latest_gross_signal_bps=signals[-1] if signals else None,
    )
    payload = {
        "version": "dynamic-carry-daily-open-replay-v1",
        "evidence_scope": "REJECTION_ONLY_DIAGNOSTIC",
        "carry_dataset_id": carry_dataset.manifest.dataset_id,
        "spot_dataset_id": spot_dataset.manifest.dataset_id,
        "funding_dataset_id": carry_dataset.manifest.funding_dataset_id,
        "policy": policy,
        "start": start,
        "end": end,
        "assumptions": (
            "UTC_DAILY_OPEN_DECISION_CLOCK",
            "FUNDING_VISIBLE_ONLY_AFTER_FROZEN_AVAILABLE_AT",
            "MISSING_SIGNAL_RETAINS_EXISTING_POSITION",
            "SAME_BASE_QUANTITY_SPOT_LONG_PERPETUAL_SHORT",
            "CURRENT_PRODUCTION_HYSTERESIS_SIZING_AND_FEES",
            "CONSERVATIVE_UNSYNCHRONIZED_DAILY_HIGH_LOW_DRAWDOWN_BOUND",
            "NO_CODEX_REPLAY",
        ),
        "limitations": (
            "DAILY_OPEN_CANNOT_REPLAY_THE_PRODUCTION_15_MINUTE_TRIGGER_CLOCK",
            "DAILY_TRADE_OPENS_ARE_NOT_EXECUTABLE_BID_ASK_QUOTES",
            "LAST_VISIBLE_SETTLEMENT_PROXIES_HISTORICAL_PERPETUAL_STATE_FUNDING_RATE",
            "INTRADAY_SIGNAL_DURATION_AND_CROSS_MARKET_QUOTE_SKEW_ARE_UNOBSERVED",
            "DIAGNOSTIC_RESULT_CANNOT_GRANT_DEPLOYMENT_PERMISSION",
        ),
        "actions": tuple(actions),
        "metrics": metrics,
    }
    return DynamicCarryReplayResult(
        result_id=stable_id("dynamic_carry_replay", content_hash(payload)),
        **payload,
    )


def _validated_window(
    *,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    policy: DynamicCarryReplayPolicy,
    start: datetime,
    end: datetime,
) -> tuple[tuple[CarryMarketDay, ...], dict[datetime, ClosedMarketBar]]:
    manifest = carry_dataset.manifest
    if (
        manifest.symbol != policy.symbol
        or spot_dataset.manifest.symbol != policy.symbol
        or manifest.spot_dataset_id != spot_dataset.manifest.dataset_id
        or spot_dataset.manifest.interval != "1d"
    ):
        raise ValueError("Dynamic Carry 诊断数据集与策略品种或周期不一致")
    spot_by_open = {item.open_time: item for item in spot_dataset.bars}
    days = tuple(item for item in carry_dataset.days if start <= item.open_time < end)
    if not days or days[0].open_time != start:
        raise ValueError("Dynamic Carry 诊断必须从冻结日线开盘开始")
    if days[-1].close_time >= end:
        raise ValueError("Dynamic Carry 诊断终点必须晚于最后一根完整日线")
    if any(item.open_time not in spot_by_open for item in days):
        raise ValueError("Dynamic Carry 诊断缺少点时对齐的 Spot 日线")
    return days, spot_by_open


def _daily_open_signal(
    *,
    carry_dataset: HistoricalCarryDataset,
    settlement_times: tuple[datetime, ...],
    policy: DynamicCarryReplayPolicy,
    at: datetime,
    spot_open: Decimal,
    contract_open: Decimal,
) -> Decimal | None:
    start = at - timedelta(hours=policy.funding_lookback_hours)
    left = bisect_left(settlement_times, start)
    right = bisect_left(settlement_times, at)
    visible = tuple(
        item for item in carry_dataset.settlements[left:right] if item.available_at <= at
    )
    if len(visible) < policy.minimum_funding_settlements:
        return None
    trailing_rate = median(item.funding_rate for item in visible)
    latest_rate = max(visible, key=lambda item: item.funding_time).funding_rate
    projected_rate = min(trailing_rate, latest_rate)
    funding_periods = Decimal(policy.forecast_horizon_hours) / Decimal(
        policy.funding_interval_hours
    )
    basis_bps = Decimal("0.5") * (contract_open / spot_open - Decimal("1")) * _BPS
    funding_bps = Decimal("0.5") * projected_rate * funding_periods * _BPS
    return basis_bps + funding_bps


def _settlement_value_between(
    *,
    settlements: tuple[CarryFundingSettlement, ...],
    settlement_times: tuple[datetime, ...],
    start: datetime,
    end: datetime,
) -> Decimal:
    left = bisect_right(settlement_times, start)
    right = bisect_right(settlement_times, end)
    return sum(
        (item.mark_price * item.funding_rate for item in settlements[left:right]),
        Decimal("0"),
    )


def _group_is_executable(
    *,
    prior_quantity: Decimal,
    target_quantity: Decimal,
    spot_price: Decimal,
    perpetual_price: Decimal,
    policy: DynamicCarryReplayPolicy,
) -> bool:
    delta = abs(target_quantity - prior_quantity)
    return bool(
        delta > 0
        and delta * spot_price >= policy.spot_minimum_notional
        and delta * perpetual_price >= policy.perpetual_minimum_notional
    )


def _execution_cost(
    *,
    quantity_delta: Decimal,
    spot_price: Decimal,
    perpetual_price: Decimal,
    policy: DynamicCarryReplayPolicy,
) -> Decimal:
    return (
        quantity_delta
        * (spot_price * policy.spot_fee_bps + perpetual_price * policy.perpetual_fee_bps)
        / _BPS
    )
