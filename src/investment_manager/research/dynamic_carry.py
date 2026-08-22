"""Point-in-time diagnostic replays for the active rolling carry hypothesis.

Daily bars provide a long-horizon diagnostic.  Aligned intraday Spot/USD-M trade
bars provide a more faithful but deliberately optimistic screen because public
history does not contain recent executable quotes for both legs.  Both paths can
reject weak economics; neither can authorize deployment.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
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


class DynamicCarryIntradayReplayPolicy(FrozenModel):
    version: Literal["dynamic-carry-intraday-trade-open-v1"] = (
        "dynamic-carry-intraday-trade-open-v1"
    )
    capital: DynamicCarryReplayPolicy
    trigger_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    heartbeat_minutes: int = Field(gt=0)
    bar_interval_minutes: int = Field(gt=0)

    @model_validator(mode="after")
    def clock_is_exactly_representable(self):
        if self.heartbeat_minutes % self.bar_interval_minutes:
            raise ValueError("盘中 Carry K 线必须精确整除 Heartbeat 周期")
        return self


class DynamicCarryIntradayReplayMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal
    net_pnl: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    return_fraction: Decimal
    simple_annualized_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    bar_count: int = Field(gt=0)
    decision_count: int = Field(gt=0)
    signal_count: int = Field(ge=0)
    missing_signal_count: int = Field(ge=0)
    entry_eligible_observation_count: int = Field(ge=0)
    exposure_bar_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    rebalance_count: int = Field(ge=0)
    signal_exit_count: int = Field(ge=0)
    boundary_exit_count: int = Field(ge=0, le=1)
    maximum_gross_signal_bps: Decimal | None = None
    latest_gross_signal_bps: Decimal | None = None

    @model_validator(mode="after")
    def metrics_reconcile(self):
        if self.ending_equity - self.starting_equity != self.net_pnl:
            raise ValueError("盘中 Dynamic Carry 权益与净损益不一致")
        if self.funding_pnl + self.basis_pnl - self.modeled_cost != self.net_pnl:
            raise ValueError("盘中 Dynamic Carry 收益归因无法核对")
        if self.signal_count + self.missing_signal_count != self.decision_count:
            raise ValueError("盘中 Dynamic Carry 决策数量无法核对")
        return self


class DynamicCarryIntradayPhaseResult(FrozenModel):
    phase_offset_minutes: int = Field(ge=0)
    actions: tuple[DynamicCarryReplayAction, ...]
    metrics: DynamicCarryIntradayReplayMetrics


class DynamicCarryIntradayReplayResult(FrozenModel):
    version: Literal["dynamic-carry-intraday-replay-v1"] = "dynamic-carry-intraday-replay-v1"
    result_id: str
    evidence_scope: Literal["REJECTION_ONLY_OPTIMISTIC_DIAGNOSTIC"] = (
        "REJECTION_ONLY_OPTIMISTIC_DIAGNOSTIC"
    )
    carry_dataset_id: str
    spot_dataset_id: str
    perpetual_dataset_id: str
    funding_dataset_id: str
    policy: DynamicCarryIntradayReplayPolicy
    start: datetime
    end: datetime
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    phases: tuple[DynamicCarryIntradayPhaseResult, ...] = Field(min_length=1)

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)

    @model_validator(mode="after")
    def identity_and_phases_match(self):
        expected_offsets = tuple(
            range(0, self.policy.heartbeat_minutes, self.policy.bar_interval_minutes)
        )
        if tuple(item.phase_offset_minutes for item in self.phases) != expected_offsets:
            raise ValueError("盘中 Dynamic Carry 必须完整覆盖全部 K 线时钟相位")
        payload = self.model_dump(exclude={"result_id"})
        expected = stable_id("dynamic_carry_intraday_replay", content_hash(payload))
        if self.result_id != expected:
            raise ValueError("盘中 Dynamic Carry 结果 ID 与内容不一致")
        return self


class DynamicCarryReplayEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: DynamicCarryReplayResult | DynamicCarryIntradayReplayResult


class DynamicCarryReplayCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(
        self,
        result: DynamicCarryReplayResult | DynamicCarryIntradayReplayResult,
    ) -> Path:
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

    def load(
        self,
        result_id: str,
    ) -> DynamicCarryReplayResult | DynamicCarryIntradayReplayResult:
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


def intraday_replay_policy_from_config(
    config: AppConfig,
    *,
    bar_interval_minutes: int,
) -> DynamicCarryIntradayReplayPolicy:
    """Bind the replay clock to the exact active heartbeat policy."""

    return DynamicCarryIntradayReplayPolicy(
        capital=replay_policy_from_config(config),
        trigger_policy_hash=content_hash(config.trigger),
        heartbeat_minutes=config.trigger.heartbeat_minutes,
        bar_interval_minutes=bar_interval_minutes,
    )


@dataclass(frozen=True, slots=True)
class _AlignedReplayBar:
    open_time: datetime
    close_time: datetime
    spot_open: Decimal
    spot_high: Decimal
    spot_low: Decimal
    spot_close: Decimal
    perpetual_open: Decimal
    perpetual_high: Decimal
    perpetual_close: Decimal


@dataclass(frozen=True, slots=True)
class _ReplayOutcome:
    ending_equity: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    modeled_cost: Decimal
    maximum_drawdown_fraction: Decimal
    observation_count: int
    decision_count: int
    signal_count: int
    missing_signal_count: int
    entry_eligible_count: int
    exposure_count: int
    entry_count: int
    rebalance_count: int
    signal_exit_count: int
    boundary_exit_count: int
    maximum_gross_signal_bps: Decimal | None
    latest_gross_signal_bps: Decimal | None
    actions: tuple[DynamicCarryReplayAction, ...]


def _run_aligned_replay(
    *,
    bars: tuple[_AlignedReplayBar, ...],
    settlements: tuple[CarryFundingSettlement, ...],
    policy: DynamicCarryReplayPolicy,
    starting_equity: Decimal,
    is_decision_time: Callable[[datetime], bool],
) -> _ReplayOutcome:
    """Single accounting implementation shared by daily and intraday clocks."""

    if starting_equity <= 0 or not bars:
        raise ValueError("Dynamic Carry 回放要求正初始权益和非空行情")
    settlement_times = tuple(item.funding_time for item in settlements)
    equity = starting_equity
    peak = equity
    maximum_drawdown = Decimal("0")
    quantity = Decimal("0")
    prior: _AlignedReplayBar | None = None
    funding_pnl = Decimal("0")
    basis_pnl = Decimal("0")
    modeled_cost = Decimal("0")
    actions: list[DynamicCarryReplayAction] = []
    signals: list[Decimal] = []
    decision_count = 0
    missing_signals = 0
    entry_eligible = 0
    exposure_count = 0
    entry_count = 0
    rebalance_count = 0
    signal_exit_count = 0

    for bar in bars:
        if prior is not None:
            basis_change = quantity * (
                (bar.spot_open - prior.spot_open) - (bar.perpetual_open - prior.perpetual_open)
            )
            funding_change = quantity * _settlement_value_between(
                settlements=settlements,
                settlement_times=settlement_times,
                start=prior.open_time,
                end=bar.open_time,
            )
            equity += basis_change + funding_change
            basis_pnl += basis_change
            funding_pnl += funding_change
            peak = max(peak, equity)
            maximum_drawdown = max(
                maximum_drawdown,
                Decimal("1") - equity / peak,
            )

        if is_decision_time(bar.open_time):
            decision_count += 1
            signal = _projected_gross_signal(
                settlements=settlements,
                settlement_times=settlement_times,
                policy=policy,
                at=bar.open_time,
                spot_open=bar.spot_open,
                contract_open=bar.perpetual_open,
            )
            if signal is None:
                missing_signals += 1
            else:
                signals.append(signal)
                net_signal = signal - policy.estimated_round_trip_cost_bps
                if net_signal >= policy.minimum_entry_net_bps:
                    entry_eligible += 1
                selected = net_signal >= (
                    policy.minimum_hold_net_bps if quantity > 0 else policy.minimum_entry_net_bps
                )
                desired_gross = (
                    equity * policy.gross_allocation_fraction if selected else Decimal("0")
                )
                current_gross = quantity * (bar.spot_open + bar.perpetual_open)
                turnover = abs(desired_gross - current_gross)
                if Decimal("0") < turnover < policy.minimum_rebalance_notional:
                    desired_gross = current_gross
                target_quantity = floor_to_step(
                    desired_gross / (bar.spot_open + bar.perpetual_open),
                    policy.common_quantity_step,
                )
                if target_quantity != quantity and not _group_is_executable(
                    prior_quantity=quantity,
                    target_quantity=target_quantity,
                    spot_price=bar.spot_open,
                    perpetual_price=bar.perpetual_open,
                    policy=policy,
                ):
                    target_quantity = quantity
                if target_quantity != quantity:
                    prior_quantity = quantity
                    cost = _execution_cost(
                        quantity_delta=abs(target_quantity - quantity),
                        spot_price=bar.spot_open,
                        perpetual_price=bar.perpetual_open,
                        policy=policy,
                    )
                    equity -= cost
                    modeled_cost += cost
                    kind: Literal[
                        "ENTRY",
                        "REBALANCE",
                        "SIGNAL_EXIT",
                        "BOUNDARY_EXIT",
                    ]
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
                            at=bar.open_time,
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
            exposure_count += 1
            conservative_intrabar_equity = equity + quantity * (
                (bar.spot_low - bar.spot_open) - (bar.perpetual_high - bar.perpetual_open)
            )
            maximum_drawdown = max(
                maximum_drawdown,
                Decimal("1") - conservative_intrabar_equity / peak,
            )
        prior = bar

    last = bars[-1]
    basis_change = quantity * (
        (last.spot_close - last.spot_open) - (last.perpetual_close - last.perpetual_open)
    )
    funding_change = quantity * _settlement_value_between(
        settlements=settlements,
        settlement_times=settlement_times,
        start=last.open_time,
        end=last.close_time,
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
            spot_price=last.spot_close,
            perpetual_price=last.perpetual_close,
            policy=policy,
        )
        equity -= closing_cost
        modeled_cost += closing_cost
        actions.append(
            DynamicCarryReplayAction(
                at=last.close_time,
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

    return _ReplayOutcome(
        ending_equity=equity,
        funding_pnl=funding_pnl,
        basis_pnl=basis_pnl,
        modeled_cost=modeled_cost,
        maximum_drawdown_fraction=maximum_drawdown,
        observation_count=len(bars),
        decision_count=decision_count,
        signal_count=len(signals),
        missing_signal_count=missing_signals,
        entry_eligible_count=entry_eligible,
        exposure_count=exposure_count,
        entry_count=entry_count,
        rebalance_count=rebalance_count,
        signal_exit_count=signal_exit_count,
        boundary_exit_count=boundary_exit_count,
        maximum_gross_signal_bps=max(signals) if signals else None,
        latest_gross_signal_bps=signals[-1] if signals else None,
        actions=tuple(actions),
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
    bars = tuple(
        _AlignedReplayBar(
            open_time=day.open_time,
            close_time=day.close_time,
            spot_open=spot_by_open[day.open_time].open,
            spot_high=spot_by_open[day.open_time].high,
            spot_low=spot_by_open[day.open_time].low,
            spot_close=spot_by_open[day.open_time].close,
            perpetual_open=day.contract_open,
            perpetual_high=day.contract_high,
            perpetual_close=day.contract_close,
        )
        for day in days
    )
    outcome = _run_aligned_replay(
        bars=bars,
        settlements=carry_dataset.settlements,
        policy=policy,
        starting_equity=starting_equity,
        is_decision_time=lambda _: True,
    )

    elapsed_days = Decimal(
        str((bars[-1].close_time - bars[0].open_time).total_seconds())
    ) / Decimal("86400")
    net_pnl = outcome.ending_equity - starting_equity
    metrics = DynamicCarryReplayMetrics(
        starting_equity=starting_equity,
        ending_equity=outcome.ending_equity,
        net_pnl=net_pnl,
        funding_pnl=outcome.funding_pnl,
        basis_pnl=outcome.basis_pnl,
        modeled_cost=outcome.modeled_cost,
        return_fraction=net_pnl / starting_equity,
        simple_annualized_return_fraction=(
            net_pnl / starting_equity * Decimal("365.25") / elapsed_days
        ),
        maximum_drawdown_fraction=outcome.maximum_drawdown_fraction,
        day_count=outcome.observation_count,
        signal_day_count=outcome.signal_count,
        missing_signal_day_count=outcome.missing_signal_count,
        entry_eligible_day_count=outcome.entry_eligible_count,
        exposure_day_count=outcome.exposure_count,
        entry_count=outcome.entry_count,
        rebalance_count=outcome.rebalance_count,
        signal_exit_count=outcome.signal_exit_count,
        boundary_exit_count=outcome.boundary_exit_count,
        maximum_gross_signal_bps=outcome.maximum_gross_signal_bps,
        latest_gross_signal_bps=outcome.latest_gross_signal_bps,
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
        "actions": outcome.actions,
        "metrics": metrics,
    }
    return DynamicCarryReplayResult(
        result_id=stable_id("dynamic_carry_replay", content_hash(payload)),
        **payload,
    )


def run_dynamic_carry_intraday_replay(
    *,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    perpetual_dataset: HistoricalDataset,
    policy: DynamicCarryIntradayReplayPolicy,
    starting_equity: Decimal,
    start: datetime,
    end: datetime,
) -> DynamicCarryIntradayReplayResult:
    """Replay every observable heartbeat phase without historical bid/ask."""

    start = require_utc(start)
    end = require_utc(end)
    if starting_equity <= 0 or start >= end:
        raise ValueError("盘中 Dynamic Carry 回放资金或时间边界非法")
    bars = _validated_intraday_window(
        carry_dataset=carry_dataset,
        spot_dataset=spot_dataset,
        perpetual_dataset=perpetual_dataset,
        policy=policy,
        start=start,
        end=end,
    )
    elapsed_days = Decimal(
        str((bars[-1].close_time - bars[0].open_time).total_seconds())
    ) / Decimal("86400")
    phases = []
    for offset in range(
        0,
        policy.heartbeat_minutes,
        policy.bar_interval_minutes,
    ):
        outcome = _run_aligned_replay(
            bars=bars,
            settlements=carry_dataset.settlements,
            policy=policy.capital,
            starting_equity=starting_equity,
            is_decision_time=lambda at, phase=offset: (
                (at.hour * 60 + at.minute - phase) % policy.heartbeat_minutes == 0
            ),
        )
        net_pnl = outcome.ending_equity - starting_equity
        phases.append(
            DynamicCarryIntradayPhaseResult(
                phase_offset_minutes=offset,
                actions=outcome.actions,
                metrics=DynamicCarryIntradayReplayMetrics(
                    starting_equity=starting_equity,
                    ending_equity=outcome.ending_equity,
                    net_pnl=net_pnl,
                    funding_pnl=outcome.funding_pnl,
                    basis_pnl=outcome.basis_pnl,
                    modeled_cost=outcome.modeled_cost,
                    return_fraction=net_pnl / starting_equity,
                    simple_annualized_return_fraction=(
                        net_pnl / starting_equity * Decimal("365.25") / elapsed_days
                    ),
                    maximum_drawdown_fraction=(outcome.maximum_drawdown_fraction),
                    bar_count=outcome.observation_count,
                    decision_count=outcome.decision_count,
                    signal_count=outcome.signal_count,
                    missing_signal_count=outcome.missing_signal_count,
                    entry_eligible_observation_count=(outcome.entry_eligible_count),
                    exposure_bar_count=outcome.exposure_count,
                    entry_count=outcome.entry_count,
                    rebalance_count=outcome.rebalance_count,
                    signal_exit_count=outcome.signal_exit_count,
                    boundary_exit_count=outcome.boundary_exit_count,
                    maximum_gross_signal_bps=(outcome.maximum_gross_signal_bps),
                    latest_gross_signal_bps=outcome.latest_gross_signal_bps,
                ),
            )
        )
    payload = {
        "version": "dynamic-carry-intraday-replay-v1",
        "evidence_scope": "REJECTION_ONLY_OPTIMISTIC_DIAGNOSTIC",
        "carry_dataset_id": carry_dataset.manifest.dataset_id,
        "spot_dataset_id": spot_dataset.manifest.dataset_id,
        "perpetual_dataset_id": perpetual_dataset.manifest.dataset_id,
        "funding_dataset_id": carry_dataset.manifest.funding_dataset_id,
        "policy": policy,
        "start": start,
        "end": end,
        "assumptions": (
            "ALL_BAR_REPRESENTABLE_HEARTBEAT_PHASES_REPLAYED",
            "FUNDING_VISIBLE_ONLY_AFTER_FROZEN_AVAILABLE_AT",
            "MISSING_SIGNAL_RETAINS_EXISTING_POSITION",
            "SAME_BASE_QUANTITY_SPOT_LONG_PERPETUAL_SHORT",
            "CURRENT_PRODUCTION_HYSTERESIS_SIZING_AND_FEES",
            "CONSERVATIVE_UNSYNCHRONIZED_INTRABAR_HIGH_LOW_DRAWDOWN_BOUND",
            "NO_CODEX_REPLAY",
        ),
        "limitations": (
            "TRADE_BAR_OPENS_ARE_NOT_EXECUTABLE_BID_ASK_QUOTES",
            "ZERO_HISTORICAL_SPREAD_MAKES_REPLAY_OPTIMISTIC",
            "LAST_VISIBLE_SETTLEMENT_PROXIES_HISTORICAL_PERPETUAL_STATE_FUNDING_RATE",
            "BAR_BUCKETS_CANNOT_REPRODUCE_EXACT_CROSS_MARKET_QUOTE_SKEW",
            "EVENT_DRIVEN_TRIGGER_TIMES_OUTSIDE_HEARTBEAT_PHASES_ARE_NOT_REPLAYED",
            "DIAGNOSTIC_RESULT_CANNOT_GRANT_DEPLOYMENT_PERMISSION",
        ),
        "phases": tuple(phases),
    }
    return DynamicCarryIntradayReplayResult(
        result_id=stable_id(
            "dynamic_carry_intraday_replay",
            content_hash(payload),
        ),
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


def _validated_intraday_window(
    *,
    carry_dataset: HistoricalCarryDataset,
    spot_dataset: HistoricalDataset,
    perpetual_dataset: HistoricalDataset,
    policy: DynamicCarryIntradayReplayPolicy,
    start: datetime,
    end: datetime,
) -> tuple[_AlignedReplayBar, ...]:
    capital = policy.capital
    if (
        carry_dataset.manifest.symbol != capital.symbol
        or spot_dataset.manifest.symbol != capital.symbol
        or perpetual_dataset.manifest.symbol != capital.symbol
        or spot_dataset.manifest.source != "binance-rest-historical"
        or perpetual_dataset.manifest.source != "binance-usdm-rest-historical"
        or spot_dataset.manifest.interval != perpetual_dataset.manifest.interval
        or spot_dataset.manifest.interval != f"{policy.bar_interval_minutes}m"
    ):
        raise ValueError("盘中 Dynamic Carry 数据源、品种或周期不一致")
    if not (
        carry_dataset.manifest.requested_start <= start
        and end <= carry_dataset.manifest.requested_end
    ):
        raise ValueError("盘中 Dynamic Carry 超出资金结算数据窗口")
    spots = tuple(item for item in spot_dataset.bars if start <= item.open_time < end)
    perpetuals = tuple(item for item in perpetual_dataset.bars if start <= item.open_time < end)
    if (
        not spots
        or len(spots) != len(perpetuals)
        or spots[0].open_time != start
        or spots[-1].close_time >= end
    ):
        raise ValueError("盘中 Dynamic Carry 窗口缺少完整对齐 K 线")
    if any(
        spot.open_time != perpetual.open_time or spot.close_time != perpetual.close_time
        for spot, perpetual in zip(spots, perpetuals, strict=True)
    ):
        raise ValueError("盘中 Dynamic Carry Spot/Perpetual K 线未点时对齐")
    return tuple(
        _AlignedReplayBar(
            open_time=spot.open_time,
            close_time=spot.close_time,
            spot_open=spot.open,
            spot_high=spot.high,
            spot_low=spot.low,
            spot_close=spot.close,
            perpetual_open=perpetual.open,
            perpetual_high=perpetual.high,
            perpetual_close=perpetual.close,
        )
        for spot, perpetual in zip(spots, perpetuals, strict=True)
    )


def _projected_gross_signal(
    *,
    settlements: tuple[CarryFundingSettlement, ...],
    settlement_times: tuple[datetime, ...],
    policy: DynamicCarryReplayPolicy,
    at: datetime,
    spot_open: Decimal,
    contract_open: Decimal,
) -> Decimal | None:
    start = at - timedelta(hours=policy.funding_lookback_hours)
    left = bisect_left(settlement_times, start)
    right = bisect_left(settlement_times, at)
    visible = tuple(item for item in settlements[left:right] if item.available_at <= at)
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
