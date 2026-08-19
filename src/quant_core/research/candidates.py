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
    Side,
    SignalCandidate,
)
from quant_core.ids import stable_id
from quant_core.research.backtest import ResearchStrategy
from quant_core.strategy import PriceTrendStrategy


class LongOnlyDualTrendSpec(FrozenModel):
    """Long/cash 28-day momentum gated by a 200-day trend regime."""

    strategy_id: str = "long-only-dual-trend"
    version: str = "long-only-dual-trend-28d-sma200-5d-v1"
    family: str = "dual-trend"
    interval: str = "1d"
    momentum_lookback_bars: int = Field(default=28, ge=2)
    regime_moving_average_bars: int = Field(default=200, ge=2)
    atr_bars: int = Field(default=30, ge=2)
    stop_atr_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    horizon_minutes: int = Field(default=7_200, gt=0)
    cooldown_minutes: int = Field(default=7_200, ge=0)
    signal_validity_minutes: int = Field(default=1_440, gt=0)
    expected_edge_half_life_seconds: int = Field(default=86_400, gt=0, le=86_400)

    @computed_field
    @property
    def required_bar_window(self) -> int:
        return max(
            self.momentum_lookback_bars + 1,
            self.regime_moving_average_bars,
            self.atr_bars + 1,
        )


class LongOnlyDualTrendStrategy:
    def __init__(self, spec: LongOnlyDualTrendSpec | None = None) -> None:
        self._spec = spec or LongOnlyDualTrendSpec()

    @property
    def research_spec(self) -> LongOnlyDualTrendSpec:
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
        previous_close = market.bars[-1 - spec.momentum_lookback_bars].close
        momentum = current_close / previous_close - Decimal("1")
        regime_average = sum(
            (
                bar.close
                for bar in market.bars[-spec.regime_moving_average_bars :]
            ),
            Decimal("0"),
        ) / Decimal(spec.regime_moving_average_bars)
        regime_distance = current_close / regime_average - Decimal("1")
        if momentum <= 0 or regime_distance <= 0:
            return ()

        stop_price = market.ask - _average_true_range(
            market.bars,
            spec.atr_bars,
        ) * spec.stop_atr_multiple
        if stop_price <= 0 or stop_price >= market.ask:
            return ()

        return (
            SignalCandidate(
                candidate_id=stable_id(
                    "sig",
                    market.cycle_id,
                    spec.strategy_id,
                    spec.version,
                    market.symbol,
                ),
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
                valid_until=market.as_of
                + timedelta(minutes=spec.signal_validity_minutes),
                signal_observed_at=market.as_of,
                reference_price=market.ask,
                expected_edge_half_life_seconds=spec.expected_edge_half_life_seconds,
                raw_score=min(Decimal("1"), momentum, regime_distance),
                expected_gross_bps=Decimal("0"),
                calibration_ref=uncalibrated_ref(spec.version),
                unknowns=(EDGE_CALIBRATION_MISSING,),
            ),
        )


def resolve_research_candidate(
    candidate: str, config: AppConfig
) -> tuple[AppConfig, ResearchStrategy]:
    """Resolve active research code; failed candidates live only in result artifacts."""

    if candidate == "configured":
        if not config.strategy.enabled:
            raise ValueError("已禁用的程序策略不能作为历史或前瞻评价基线")
        return config, PriceTrendStrategy(config.strategy)
    if candidate == "long-only-dual-trend-28d-sma200-5d-v1":
        spec = LongOnlyDualTrendSpec()
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
                        "version": "five-day-entry-research-v1",
                        "cooldown_minutes": spec.cooldown_minutes,
                        "maximum_orders_per_day": 1,
                    }
                ),
            }
        )
        return effective, LongOnlyDualTrendStrategy(spec)
    raise ValueError(f"未知或已退役的历史候选: {candidate}")


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
