from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pydantic import Field, computed_field

from quant_core.calibration import EDGE_CALIBRATION_MISSING, uncalibrated_ref
from quant_core.config import AppConfig
from quant_core.domain import (
    AccountSnapshot,
    Action,
    FeatureSnapshot,
    FrozenModel,
    IntelligenceEvent,
    MarketBar,
    MarketSnapshot,
    OrderType,
    PriceCondition,
    ProgramExitCondition,
    Side,
    SignalCandidate,
)
from quant_core.ids import stable_id
from quant_core.research.backtest import ResearchStrategy
from quant_core.strategy import PriceTrendStrategy


class LongOnlyTimeSeriesMomentumSpec(FrozenModel):
    """Pre-registered long/cash adaptation of 12-month time-series momentum."""

    strategy_id: str = "long-only-tsmom"
    version: str = "long-only-tsmom-12m-v1"
    family: str = "time-series-momentum"
    interval: str = "1d"
    lookback_bars: int = Field(default=365, ge=2)
    atr_bars: int = Field(default=30, ge=2)
    stop_atr_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    horizon_minutes: int = Field(default=43_200, gt=0)
    cooldown_minutes: int = Field(default=43_200, ge=0)
    signal_validity_minutes: int = Field(default=1_440, gt=0)
    expected_edge_half_life_seconds: int = Field(default=86_400, gt=0, le=86_400)

    @computed_field
    @property
    def required_bar_window(self) -> int:
        return max(self.lookback_bars + 1, self.atr_bars + 1)


class LongOnlyTimeSeriesMomentumStrategy:
    def __init__(self, spec: LongOnlyTimeSeriesMomentumSpec | None = None) -> None:
        self._spec = spec or LongOnlyTimeSeriesMomentumSpec()

    @property
    def research_spec(self) -> LongOnlyTimeSeriesMomentumSpec:
        return self._spec

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]:
        spec = self._spec
        if len(market.bars) < spec.required_bar_window:
            return ()
        if any(
            position.symbol == market.symbol and position.quantity > 0
            for position in account.positions
        ):
            return ()

        current_close = market.bars[-1].close
        previous_close = market.bars[-1 - spec.lookback_bars].close
        momentum = current_close / previous_close - Decimal("1")
        if momentum <= 0:
            return ()

        atr = _average_true_range(market.bars, spec.atr_bars)
        stop_price = market.ask - atr * spec.stop_atr_multiple
        if stop_price <= 0 or stop_price >= market.ask:
            return ()

        candidate_id = stable_id(
            "sig",
            market.cycle_id,
            spec.strategy_id,
            spec.version,
            market.symbol,
        )
        return (
            SignalCandidate(
                candidate_id=candidate_id,
                cycle_id=market.cycle_id,
                producer_id=spec.strategy_id,
                producer_version=spec.version,
                strategy_family=spec.family,
                symbol=market.symbol,
                action=Action.OPEN,
                side=Side.BUY,
                horizon_minutes=spec.horizon_minutes,
                feature_refs=(features.feature_set_version,),
                entry=PriceCondition(order_type=OrderType.MARKET),
                stop_price=stop_price,
                valid_until=market.as_of + timedelta(minutes=spec.signal_validity_minutes),
                signal_observed_at=market.as_of,
                reference_price=market.ask,
                expected_edge_half_life_seconds=spec.expected_edge_half_life_seconds,
                raw_score=min(Decimal("1"), momentum),
                expected_gross_bps=Decimal("0"),
                calibration_ref=uncalibrated_ref(spec.version),
                unknowns=(EDGE_CALIBRATION_MISSING,),
            ),
        )


class LongOnlyMovingAverageSpec(FrozenModel):
    """Pre-registered low-turnover moving-average state filter."""

    strategy_id: str = "long-only-sma"
    version: str = "long-only-sma100-2w-v1"
    family: str = "moving-average-trend"
    interval: str = "1d"
    moving_average_bars: int = Field(default=100, ge=2)
    atr_bars: int = Field(default=30, ge=2)
    stop_atr_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    horizon_minutes: int = Field(default=20_160, gt=0)
    cooldown_minutes: int = Field(default=20_160, ge=0)
    program_exit_moving_average_bars: int | None = Field(default=None, ge=2)
    signal_validity_minutes: int = Field(default=1_440, gt=0)
    expected_edge_half_life_seconds: int = Field(default=86_400, gt=0, le=86_400)

    @computed_field
    @property
    def required_bar_window(self) -> int:
        return max(
            self.moving_average_bars,
            self.atr_bars + 1,
            self.program_exit_moving_average_bars or 0,
        )


class LongOnlyMovingAverageStrategy:
    def __init__(self, spec: LongOnlyMovingAverageSpec | None = None) -> None:
        self._spec = spec or LongOnlyMovingAverageSpec()

    @property
    def research_spec(self) -> LongOnlyMovingAverageSpec:
        return self._spec

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]:
        spec = self._spec
        if len(market.bars) < spec.required_bar_window:
            return ()
        if any(
            position.symbol == market.symbol and position.quantity > 0
            for position in account.positions
        ):
            return ()

        current_close = market.bars[-1].close
        moving_average = sum(
            (bar.close for bar in market.bars[-spec.moving_average_bars :]),
            Decimal("0"),
        ) / Decimal(spec.moving_average_bars)
        distance = current_close / moving_average - Decimal("1")
        if distance <= 0:
            return ()

        atr = _average_true_range(market.bars, spec.atr_bars)
        stop_price = market.ask - atr * spec.stop_atr_multiple
        if stop_price <= 0 or stop_price >= market.ask:
            return ()

        candidate_id = stable_id(
            "sig",
            market.cycle_id,
            spec.strategy_id,
            spec.version,
            market.symbol,
        )
        return (
            SignalCandidate(
                candidate_id=candidate_id,
                cycle_id=market.cycle_id,
                producer_id=spec.strategy_id,
                producer_version=spec.version,
                strategy_family=spec.family,
                symbol=market.symbol,
                action=Action.OPEN,
                side=Side.BUY,
                horizon_minutes=spec.horizon_minutes,
                feature_refs=(features.feature_set_version,),
                entry=PriceCondition(order_type=OrderType.MARKET),
                stop_price=stop_price,
                valid_until=market.as_of + timedelta(minutes=spec.signal_validity_minutes),
                signal_observed_at=market.as_of,
                reference_price=market.ask,
                expected_edge_half_life_seconds=spec.expected_edge_half_life_seconds,
                raw_score=min(Decimal("1"), distance),
                expected_gross_bps=Decimal("0"),
                calibration_ref=uncalibrated_ref(spec.version),
                program_exit=(
                    ProgramExitCondition(
                        version=f"{spec.version}-program-exit-v1",
                        bar_interval_minutes=_interval_minutes(spec.interval),
                        moving_average_bars=spec.program_exit_moving_average_bars,
                    )
                    if spec.program_exit_moving_average_bars is not None
                    else None
                ),
                unknowns=(EDGE_CALIBRATION_MISSING,),
            ),
        )


def resolve_research_candidate(
    candidate: str, config: AppConfig
) -> tuple[AppConfig, ResearchStrategy]:
    """Resolve a named frozen candidate and its point-in-time evaluation policy."""

    if candidate == "configured":
        return config, PriceTrendStrategy(config.strategy)
    if candidate == "long-only-tsmom-12m-v1":
        spec = LongOnlyTimeSeriesMomentumSpec()
        strategy: ResearchStrategy = LongOnlyTimeSeriesMomentumStrategy(spec)
    elif candidate == "long-only-sma100-2w-v1":
        spec = LongOnlyMovingAverageSpec()
        strategy = LongOnlyMovingAverageStrategy(spec)
    elif candidate == "long-only-sma200-1m-v1":
        spec = LongOnlyMovingAverageSpec(
            version="long-only-sma200-1m-v1",
            moving_average_bars=200,
            atr_bars=30,
            stop_atr_multiple=Decimal("3"),
            horizon_minutes=43_200,
            cooldown_minutes=43_200,
        )
        strategy = LongOnlyMovingAverageStrategy(spec)
    else:
        raise ValueError(f"未知历史候选: {candidate}")
    effective = config.model_copy(
        update={
            "market_data": config.market_data.model_copy(
                update={
                    "version": "binance-public-daily-research-v1",
                    "interval": spec.interval,
                    "bar_window": spec.required_bar_window,
                }
            ),
            "feature": config.feature.model_copy(
                update={
                    "version": "daily-feature-research-v1",
                    "volatility_window": spec.atr_bars,
                }
            ),
            "frequency": config.frequency.model_copy(
                update={
                    "version": "monthly-entry-research-v1",
                    "cooldown_minutes": spec.cooldown_minutes,
                    "maximum_orders_per_day": 1,
                }
            ),
        }
    )
    return effective, strategy


def _average_true_range(bars: tuple[MarketBar, ...], window: int) -> Decimal:
    selected = bars[-window:]
    previous_close = bars[-window - 1].close
    ranges: list[Decimal] = []
    for bar in selected:
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
        previous_close = bar.close
    return sum(ranges, Decimal("0")) / Decimal(window)


def _interval_minutes(interval: str) -> int:
    value = int(interval[:-1])
    unit = interval[-1]
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1_440
    raise ValueError(f"不支持的程序退出 K 线周期: {interval}")
