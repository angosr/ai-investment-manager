from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from investment_manager.config import AppConfig
from investment_manager.decision import estimate_round_trip_cost_amount
from investment_manager.domain import (
    AccountSnapshot,
    Action,
    FrozenModel,
    IntelligenceEvent,
    MarketBar,
    MarketSnapshot,
    OrderType,
    Side,
    _require_utc,
)
from investment_manager.features import FeatureEngine
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.research.dataset import (
    HistoricalBarWindow,
    HistoricalDataset,
    HistoricalEventDataset,
)
from investment_manager.strategy import Strategy

RAW_SIGNAL_SCREEN_VERSION = "raw-signal-opportunity-screen-v1"


class ScreenableResearchStrategy(Strategy, Protocol):
    @property
    def research_spec(self) -> object: ...


class SignalScreenStatistics(FrozenModel):
    sample_count: int = Field(ge=0)
    win_rate: Decimal | None = Field(default=None, ge=0, le=1)
    mean_gross_return_bps: Decimal | None = None
    mean_modeled_cost_bps: Decimal | None = Field(default=None, ge=0)
    mean_net_return_bps: Decimal | None = None
    net_return_bps_lower_bound: Decimal | None = None
    net_return_bps_upper_bound: Decimal | None = None


class SignalScreenExample(FrozenModel):
    signal_at: datetime
    entry_at: datetime
    exit_at: datetime
    net_return_bps: Decimal

    _utc_signal = field_validator("signal_at")(_require_utc)
    _utc_entry = field_validator("entry_at")(_require_utc)
    _utc_exit = field_validator("exit_at")(_require_utc)

    @model_validator(mode="after")
    def times_are_ordered(self):
        if not self.signal_at < self.entry_at < self.exit_at:
            raise ValueError("快速筛选样本时间顺序非法")
        return self


class SignalScreenResult(FrozenModel):
    screen_id: str
    version: str = RAW_SIGNAL_SCREEN_VERSION
    dataset_id: str
    event_dataset_id: str | None = None
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opportunity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_spec_snapshot: dict[str, object]
    signal_start: datetime
    signal_end: datetime
    horizon_minutes: int = Field(gt=0)
    spread_bps: Decimal = Field(ge=0)
    minimum_non_overlapping_samples: int = Field(ge=2)
    minimum_net_return_bps_lower_bound: Decimal
    minimum_incremental_return_bps_lower_bound: Decimal
    confidence_z: Decimal = Field(gt=0)
    raw_signal_count: int = Field(ge=0)
    expired_before_entry_count: int = Field(ge=0)
    non_overlapping_signal_count: int = Field(ge=0)
    overlap_fraction: Decimal = Field(ge=0, le=1)
    signal_statistics: SignalScreenStatistics
    unconditional_statistics: SignalScreenStatistics
    incremental_net_return_bps_lower_bound: Decimal | None = None
    promising_for_exact_backtest: bool
    reason_codes: tuple[str, ...]
    examples: tuple[SignalScreenExample, ...] = ()
    limitations: tuple[str, ...]

    _utc_start = field_validator("signal_start")(_require_utc)
    _utc_end = field_validator("signal_end")(_require_utc)

    @model_validator(mode="after")
    def identity_and_summary_match(self):
        if self.signal_start >= self.signal_end:
            raise ValueError("快速筛选窗口起点必须早于终点")
        if self.non_overlapping_signal_count > self.raw_signal_count:
            raise ValueError("非重叠信号数不能超过原始信号数")
        if self.signal_statistics.sample_count != self.non_overlapping_signal_count:
            raise ValueError("信号统计样本数与非重叠信号数不一致")
        if self.promising_for_exact_backtest != (
            self.reason_codes == ("ELIGIBLE_FOR_EXACT_BACKTEST",)
        ):
            raise ValueError("快速筛选结论与原因码不一致")
        payload = self.model_dump(mode="json", exclude={"screen_id"})
        if self.screen_id != stable_id("raw_signal_screen", content_hash(payload)):
            raise ValueError("快速筛选结果 ID 与内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class _Opportunity:
    signal_at: datetime
    entry_at: datetime
    exit_at: datetime
    gross_return_bps: Decimal
    modeled_cost_bps: Decimal
    net_return_bps: Decimal


def run_raw_signal_screen(
    *,
    dataset: HistoricalDataset | HistoricalBarWindow,
    config: AppConfig,
    strategy: ScreenableResearchStrategy,
    signal_start: datetime,
    signal_end: datetime,
    event_dataset: HistoricalEventDataset | None = None,
    spread_bps: Decimal = Decimal("1"),
    minimum_non_overlapping_samples: int = 30,
    minimum_net_return_bps_lower_bound: Decimal = Decimal("0"),
    minimum_incremental_return_bps_lower_bound: Decimal = Decimal("0"),
) -> SignalScreenResult:
    """Cheaply reject weak raw signals before exact execution replay.

    This is deliberately not a portfolio backtest. It evaluates each program signal
    from the next bar open to its fixed horizon, deducts the shared round-trip cost
    model, and compares non-overlapping signals with an unconditional periodic-long
    baseline. A positive result only permits spending resources on the exact,
    preregistered Nautilus walk-forward; it never grants trading eligibility.
    """

    start = _require_utc(signal_start)
    end = _require_utc(signal_end)
    if start >= end:
        raise ValueError("快速筛选窗口起点必须早于终点")
    if spread_bps < 0:
        raise ValueError("快速筛选价差不能为负")
    if minimum_non_overlapping_samples < 2:
        raise ValueError("快速筛选至少需要 2 个非重叠样本")
    if dataset.manifest.interval != config.market_data.interval:
        raise ValueError("历史数据周期必须与冻结 MarketDataPolicy 一致")
    if dataset.manifest.symbol not in config.risk.symbol_allowlist:
        raise ValueError("快速筛选品种不在当前 RiskPolicy allowlist")
    if start < dataset.manifest.first_open_time or end > dataset.manifest.requested_end:
        raise ValueError("快速筛选窗口超出历史行情数据集")
    if event_dataset is not None and (
        event_dataset.manifest.requested_start > start
        or event_dataset.manifest.requested_end < end
    ):
        raise ValueError("历史事件数据集必须覆盖完整快速筛选窗口")

    strategy_spec = strategy.research_spec
    if not isinstance(strategy_spec, BaseModel):
        raise ValueError("快速筛选策略 research_spec 必须是可序列化的 Pydantic 模型")
    horizon_minutes = getattr(strategy_spec, "horizon_minutes", None)
    if not isinstance(horizon_minutes, int) or horizon_minutes <= 0:
        raise ValueError("快速筛选策略必须声明正整数 horizon_minutes")
    interval_minutes = config.market_data.interval_seconds // 60
    horizon_bars = _ceil_div(horizon_minutes, interval_minutes)

    bars = dataset.bars
    close_times = tuple(item.close_time for item in bars)
    first_index = max(
        config.market_data.bar_window - 1,
        bisect_left(close_times, start),
    )
    stop_index = bisect_left(close_times, end)
    if first_index >= stop_index:
        raise ValueError("快速筛选窗口扣除特征预热后没有可评价 K 线")

    observed_events: list[IntelligenceEvent] = []
    event_cursor = 0
    events = event_dataset.events if event_dataset is not None else ()
    market_bars = tuple(item.to_market_bar() for item in bars)
    raw: list[_Opportunity] = []
    unconditional: list[_Opportunity] = []
    expired_before_entry_count = 0
    unsupported_reasons: set[str] = set()
    feature_engine = FeatureEngine(config.feature)

    for index in range(first_index, stop_index):
        entry_index = index + 1
        exit_index = entry_index + horizon_bars
        if exit_index >= len(bars):
            break
        bar = bars[index]
        entry = bars[entry_index]
        exit_bar = bars[exit_index]
        # The end boundary is also the label boundary. This prevents a development
        # screen from peeking into a tail which may later be reserved for blind use.
        if exit_bar.open_time >= end:
            break
        unconditional.append(
            _opportunity(
                signal_at=bar.observed_at,
                entry_at=entry.open_time,
                exit_at=exit_bar.open_time,
                entry_price=entry.open,
                exit_price=exit_bar.open,
                spread_bps=spread_bps,
                config=config,
            )
        )

        while event_cursor < len(events) and events[event_cursor].observed_at <= bar.observed_at:
            event = events[event_cursor]
            event_cursor += 1
            if dataset.manifest.symbol in event.symbols:
                observed_events.append(event)
        if observed_events:
            observed_events = sorted(
                observed_events,
                key=lambda item: (item.event_time, item.evidence_id),
                reverse=True,
            )[:100]
        visible_events = tuple(
            sorted(
                observed_events,
                key=lambda item: (item.event_time, item.evidence_id),
            )
        )
        market = _market_snapshot(
            dataset=dataset,
            market_bars=market_bars,
            index=index,
            window=config.market_data.bar_window,
            spread_bps=spread_bps,
        )
        account = AccountSnapshot(
            cycle_id=market.cycle_id,
            as_of=market.as_of,
            observed_at=market.as_of,
            quote_balance=Decimal("10000"),
            equity=Decimal("10000"),
            equity_high_water=Decimal("10000"),
        )
        candidates = strategy.evaluate(
            market=market,
            account=account,
            features=feature_engine.compute(market),
            events=visible_events,
        )
        if not candidates:
            continue
        if len(candidates) != 1:
            unsupported_reasons.add("MULTIPLE_PROGRAM_CANDIDATES_UNSCREENABLE")
            continue
        candidate = candidates[0]
        if candidate.action != Action.OPEN or candidate.side != Side.BUY:
            unsupported_reasons.add("ONLY_LONG_OPEN_SIGNALS_SCREENABLE")
            continue
        if candidate.entry.order_type != OrderType.MARKET:
            unsupported_reasons.add("ONLY_MARKET_ENTRY_SIGNALS_SCREENABLE")
            continue
        if candidate.horizon_minutes != horizon_minutes:
            unsupported_reasons.add("CANDIDATE_HORIZON_MISMATCH")
            continue
        if candidate.signal_observed_at != market.as_of:
            unsupported_reasons.add("CANDIDATE_SIGNAL_CLOCK_MISMATCH")
            continue
        if candidate.valid_until <= entry.open_time:
            expired_before_entry_count += 1
            continue
        raw.append(
            _opportunity(
                signal_at=candidate.signal_observed_at,
                entry_at=entry.open_time,
                exit_at=exit_bar.open_time,
                entry_price=entry.open,
                exit_price=exit_bar.open,
                spread_bps=spread_bps,
                config=config,
            )
        )

    non_overlapping = _non_overlapping(tuple(raw))
    baseline_non_overlapping = _non_overlapping(tuple(unconditional))
    z = config.calibration.lower_confidence_z
    signal_statistics = _statistics(non_overlapping, z=z)
    baseline_statistics = _statistics(baseline_non_overlapping, z=z)
    incremental_lower_bound = None
    if (
        signal_statistics.net_return_bps_lower_bound is not None
        and baseline_statistics.net_return_bps_upper_bound is not None
    ):
        incremental_lower_bound = (
            signal_statistics.net_return_bps_lower_bound
            - baseline_statistics.net_return_bps_upper_bound
        )

    reasons = sorted(unsupported_reasons)
    if not raw:
        reasons.append("NO_SCREENABLE_SIGNALS")
    if len(non_overlapping) < minimum_non_overlapping_samples:
        reasons.append("NON_OVERLAPPING_SAMPLE_MINIMUM_NOT_MET")
    if (
        signal_statistics.net_return_bps_lower_bound is None
        or signal_statistics.net_return_bps_lower_bound
        <= minimum_net_return_bps_lower_bound
    ):
        reasons.append("NET_RETURN_LOWER_BOUND_NOT_ABOVE_GATE")
    if (
        incremental_lower_bound is None
        or incremental_lower_bound <= minimum_incremental_return_bps_lower_bound
    ):
        reasons.append("INCREMENTAL_RETURN_LOWER_BOUND_NOT_ABOVE_GATE")
    reason_codes = tuple(sorted(set(reasons))) or ("ELIGIBLE_FOR_EXACT_BACKTEST",)

    strategy_snapshot = strategy_spec.model_dump(mode="json")
    artifact = content_hash(
        {
            "version": RAW_SIGNAL_SCREEN_VERSION,
            "feature": config.feature,
            "market_data": {
                "version": config.market_data.version,
                "interval": config.market_data.interval,
                "bar_window": config.market_data.bar_window,
            },
            "strategy": strategy_snapshot,
            "frequency": config.frequency,
            "execution": config.execution,
            "dataset_id": dataset.manifest.dataset_id,
            "event_dataset_id": (
                event_dataset.manifest.dataset_id if event_dataset is not None else None
            ),
            "signal_start": start,
            "signal_end": end,
            "spread_bps": spread_bps,
            "minimum_non_overlapping_samples": minimum_non_overlapping_samples,
            "minimum_net_return_bps_lower_bound": (
                minimum_net_return_bps_lower_bound
            ),
            "minimum_incremental_return_bps_lower_bound": (
                minimum_incremental_return_bps_lower_bound
            ),
            "confidence_z": z,
        }
    )
    opportunity_hash = content_hash(
        tuple(
            (
                item.signal_at,
                item.entry_at,
                item.exit_at,
                item.gross_return_bps,
                item.modeled_cost_bps,
                item.net_return_bps,
            )
            for item in raw
        )
    )
    examples = _examples(non_overlapping)
    payload = {
        "version": RAW_SIGNAL_SCREEN_VERSION,
        "dataset_id": dataset.manifest.dataset_id,
        "event_dataset_id": (
            event_dataset.manifest.dataset_id if event_dataset is not None else None
        ),
        "artifact_hash": artifact,
        "opportunity_hash": opportunity_hash,
        "strategy_spec_snapshot": strategy_snapshot,
        "signal_start": start,
        "signal_end": end,
        "horizon_minutes": horizon_minutes,
        "spread_bps": spread_bps,
        "minimum_non_overlapping_samples": minimum_non_overlapping_samples,
        "minimum_net_return_bps_lower_bound": minimum_net_return_bps_lower_bound,
        "minimum_incremental_return_bps_lower_bound": (
            minimum_incremental_return_bps_lower_bound
        ),
        "confidence_z": z,
        "raw_signal_count": len(raw),
        "expired_before_entry_count": expired_before_entry_count,
        "non_overlapping_signal_count": len(non_overlapping),
        "overlap_fraction": (
            Decimal("1") - Decimal(len(non_overlapping)) / Decimal(len(raw))
            if raw
            else Decimal("0")
        ),
        "signal_statistics": signal_statistics,
        "unconditional_statistics": baseline_statistics,
        "incremental_net_return_bps_lower_bound": incremental_lower_bound,
        "promising_for_exact_backtest": reason_codes
        == ("ELIGIBLE_FOR_EXACT_BACKTEST",),
        "reason_codes": reason_codes,
        "examples": examples,
        "limitations": (
            "RAW_SIGNAL_OPPORTUNITY_SCREEN_ONLY",
            "NEXT_BAR_OPEN_TO_FIXED_HORIZON",
            "STOP_AND_PROGRAM_EXIT_NOT_REPLAYED",
            "RISK_POSITION_FREQUENCY_AND_DRAWDOWN_NOT_REPLAYED",
            "UNCONDITIONAL_BASELINE_IS_PERIODIC_LONG_WITH_IDENTICAL_COST_MODEL",
            "MAY_REJECT_OR_PRIORITIZE_BUT_CANNOT_GRANT_TRADING_ELIGIBILITY",
            "EXACT_PREREGISTERED_WALK_FORWARD_AND_BLIND_EVALUATION_STILL_REQUIRED",
            "NO_CODEX_REPLAY",
        ),
    }
    return SignalScreenResult(
        screen_id=stable_id("raw_signal_screen", content_hash(payload)),
        **payload,
    )


def _market_snapshot(
    *,
    dataset: HistoricalDataset | HistoricalBarWindow,
    market_bars: tuple[MarketBar, ...],
    index: int,
    window: int,
    spread_bps: Decimal,
) -> MarketSnapshot:
    bar = dataset.bars[index]
    half_spread = spread_bps / Decimal("20000")
    return MarketSnapshot(
        cycle_id=stable_id("signal_screen_cycle", dataset.manifest.dataset_id, bar.observed_at),
        symbol=dataset.manifest.symbol,
        as_of=bar.observed_at,
        observed_at=bar.observed_at,
        bid=bar.close * (Decimal("1") - half_spread),
        ask=bar.close * (Decimal("1") + half_spread),
        last=bar.close,
        bars=market_bars[index - window + 1 : index + 1],
        source="historical-raw-signal-screen",
    )


def _opportunity(
    *,
    signal_at: datetime,
    entry_at: datetime,
    exit_at: datetime,
    entry_price: Decimal,
    exit_price: Decimal,
    spread_bps: Decimal,
    config: AppConfig,
) -> _Opportunity:
    gross_bps = (exit_price / entry_price - Decimal("1")) * Decimal("10000")
    cost_amount = estimate_round_trip_cost_amount(
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=Decimal("1"),
        entry_order_type=OrderType.MARKET,
        spread_bps=spread_bps,
        frequency=config.frequency,
        execution=config.execution,
    )
    cost_bps = cost_amount / entry_price * Decimal("10000")
    return _Opportunity(
        signal_at=signal_at,
        entry_at=entry_at,
        exit_at=exit_at,
        gross_return_bps=gross_bps,
        modeled_cost_bps=cost_bps,
        net_return_bps=gross_bps - cost_bps,
    )


def _non_overlapping(values: tuple[_Opportunity, ...]) -> tuple[_Opportunity, ...]:
    selected: list[_Opportunity] = []
    last_exit: datetime | None = None
    for item in values:
        if last_exit is not None and item.entry_at < last_exit:
            continue
        selected.append(item)
        last_exit = item.exit_at
    return tuple(selected)


def _statistics(
    values: tuple[_Opportunity, ...], *, z: Decimal
) -> SignalScreenStatistics:
    if not values:
        return SignalScreenStatistics(sample_count=0)
    count = Decimal(len(values))
    gross = tuple(item.gross_return_bps for item in values)
    costs = tuple(item.modeled_cost_bps for item in values)
    net = tuple(item.net_return_bps for item in values)
    mean_gross = sum(gross, Decimal("0")) / count
    mean_cost = sum(costs, Decimal("0")) / count
    mean_net = sum(net, Decimal("0")) / count
    lower = upper = None
    if len(net) >= 2:
        variance = sum(((item - mean_net) ** 2 for item in net), Decimal("0")) / (
            count - Decimal("1")
        )
        margin = z * (variance / count).sqrt()
        lower = mean_net - margin
        upper = mean_net + margin
    return SignalScreenStatistics(
        sample_count=len(values),
        win_rate=Decimal(sum(item > 0 for item in net)) / count,
        mean_gross_return_bps=mean_gross,
        mean_modeled_cost_bps=mean_cost,
        mean_net_return_bps=mean_net,
        net_return_bps_lower_bound=lower,
        net_return_bps_upper_bound=upper,
    )


def _examples(values: tuple[_Opportunity, ...]) -> tuple[SignalScreenExample, ...]:
    if not values:
        return ()
    ordered = sorted(values, key=lambda item: (item.net_return_bps, item.signal_at))
    chosen = ordered[:3] + ordered[-3:]
    unique = {item.signal_at: item for item in chosen}
    return tuple(
        SignalScreenExample(
            signal_at=item.signal_at,
            entry_at=item.entry_at,
            exit_at=item.exit_at,
            net_return_bps=item.net_return_bps,
        )
        for item in sorted(unique.values(), key=lambda item: item.signal_at)
    )


def _ceil_div(left: int, right: int) -> int:
    return (left + right - 1) // right
