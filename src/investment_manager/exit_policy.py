from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

from investment_manager.domain import MarketSnapshot, ProgramExitCondition


def program_exit_triggered(
    condition: ProgramExitCondition | None,
    market: MarketSnapshot,
) -> bool:
    """Evaluate one frozen de-risking condition from closed, interval-matched bars."""

    if condition is None or len(market.bars) < condition.moving_average_bars:
        return False
    selected = market.bars[-condition.moving_average_bars :]
    expected = timedelta(minutes=condition.bar_interval_minutes)
    if any(
        current.event_time - previous.event_time != expected
        for previous, current in pairwise(selected)
    ):
        return False
    average = sum((bar.close for bar in selected), Decimal("0")) / Decimal(
        condition.moving_average_bars
    )
    return selected[-1].close < average
