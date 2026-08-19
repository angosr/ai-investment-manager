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


class LongOnlyVolatilityDipSpec(FrozenModel):
    """Buy a volatility-normalized daily pullback only inside a long-term uptrend."""

    strategy_id: str = "long-only-volatility-dip"
    version: str = "long-only-volatility-dip-sma200-3d-v1"
    family: str = "trend-filtered-dip-reversal"
    interval: str = "1d"
    regime_moving_average_bars: int = Field(default=200, ge=2)
    volatility_bars: int = Field(default=20, ge=2)
    entry_z_threshold: Decimal = Field(default=Decimal("1.5"), gt=0)
    atr_bars: int = Field(default=30, ge=2)
    stop_atr_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    horizon_minutes: int = Field(default=4_320, gt=0)
    cooldown_minutes: int = Field(default=4_320, ge=0)
    signal_validity_minutes: int = Field(default=1_440, gt=0)
    expected_edge_half_life_seconds: int = Field(default=86_400, gt=0, le=86_400)

    @computed_field
    @property
    def required_bar_window(self) -> int:
        return max(
            self.regime_moving_average_bars,
            self.volatility_bars + 2,
            self.atr_bars + 1,
        )


class LongOnlyVolatilityDipStrategy:
    def __init__(self, spec: LongOnlyVolatilityDipSpec | None = None) -> None:
        self._spec = spec or LongOnlyVolatilityDipSpec()

    @property
    def research_spec(self) -> LongOnlyVolatilityDipSpec:
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
        regime_average = sum(
            (
                bar.close
                for bar in market.bars[-spec.regime_moving_average_bars :]
            ),
            Decimal("0"),
        ) / Decimal(spec.regime_moving_average_bars)
        if current_close <= regime_average:
            return ()

        current_return = current_close / market.bars[-2].close - Decimal("1")
        if current_return >= 0:
            return ()
        prior_bars = market.bars[-(spec.volatility_bars + 2) : -1]
        prior_returns = tuple(
            prior_bars[index].close / prior_bars[index - 1].close - Decimal("1")
            for index in range(1, len(prior_bars))
        )
        prior_mean = sum(prior_returns, Decimal("0")) / Decimal(len(prior_returns))
        prior_variance = sum(
            ((value - prior_mean) ** 2 for value in prior_returns),
            Decimal("0"),
        ) / Decimal(len(prior_returns))
        prior_volatility = prior_variance.sqrt()
        if prior_volatility <= 0:
            return ()
        pullback_z = -current_return / prior_volatility
        if pullback_z < spec.entry_z_threshold:
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
                raw_score=min(Decimal("1"), pullback_z / spec.entry_z_threshold),
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
    if candidate == "long-only-volatility-dip-sma200-3d-v1":
        spec = LongOnlyVolatilityDipSpec()
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
                        "version": "three-day-entry-research-v1",
                        "cooldown_minutes": spec.cooldown_minutes,
                        "maximum_orders_per_day": 1,
                    }
                ),
            }
        )
        return effective, LongOnlyVolatilityDipStrategy(spec)
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
