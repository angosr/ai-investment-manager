from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from investment_manager.calibration import EDGE_CALIBRATION_MISSING, uncalibrated_ref
from investment_manager.config import StrategyPolicy
from investment_manager.domain import (
    AccountSnapshot,
    Action,
    IntelligenceEvent,
    OrderType,
    PriceCondition,
    Side,
    SignalCandidate,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import (
    FeatureSnapshot,
    MarketSnapshot,
)


class Strategy(Protocol):
    """Programmatic candidate producer; it cannot query, size risk, or execute orders."""

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]: ...


class PriceTrendStrategy:
    def __init__(self, policy: StrategyPolicy) -> None:
        self._policy = policy

    @property
    def research_spec(self) -> StrategyPolicy:
        """Frozen identity used only by deterministic historical evaluation."""

        return self._policy

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]:
        if not self._policy.enabled:
            return ()
        if features.regime != "TRENDING_UP":
            return ()
        if any(
            position.symbol == market.symbol and position.quantity > 0
            for position in account.positions
        ):
            return ()

        # 1% 的窗口收益映射为满分；阈值只负责候选资格，不参与缩放。
        raw_score = min(Decimal("1"), abs(features.return_fraction) * Decimal("100"))
        if raw_score < self._policy.score_threshold:
            return ()
        stop_price = market.ask - features.atr * self._policy.stop_atr_multiple
        if stop_price <= 0 or stop_price >= market.ask:
            return ()
        candidate_id = stable_id(
            "sig",
            market.cycle_id,
            self._policy.strategy_id,
            self._policy.version,
            market.symbol,
        )
        return (
            SignalCandidate(
                candidate_id=candidate_id,
                cycle_id=market.cycle_id,
                producer_id=self._policy.strategy_id,
                producer_version=self._policy.version,
                strategy_family=self._policy.family,
                symbol=market.symbol,
                action=Action.OPEN,
                side=Side.BUY,
                horizon_minutes=self._policy.horizon_minutes,
                feature_refs=(features.feature_set_version,),
                entry=PriceCondition(order_type=OrderType.MARKET),
                stop_price=stop_price,
                valid_until=market.as_of + timedelta(minutes=15),
                signal_observed_at=market.as_of,
                reference_price=market.ask,
                expected_edge_half_life_seconds=(self._policy.expected_edge_half_life_seconds),
                raw_score=raw_score,
                expected_gross_bps=Decimal("0"),
                calibration_ref=uncalibrated_ref(self._policy.version),
                unknowns=(EDGE_CALIBRATION_MISSING,),
            ),
        )
