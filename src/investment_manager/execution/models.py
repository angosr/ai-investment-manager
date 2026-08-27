from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator

from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    UnitInterval,
)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Position(FrozenModel):
    symbol: str
    quantity: Decimal
    average_price: Money


class AccountSnapshot(FrozenModel):
    cycle_id: str
    as_of: datetime
    observed_at: datetime
    quote_balance: Money
    positions: tuple[Position, ...] = ()
    open_order_count: int = Field(default=0, ge=0)
    daily_pnl: Decimal = Decimal("0")
    drawdown_fraction: UnitInterval = Decimal("0")
    equity: Money | None = None
    equity_high_water: Money | None = None
    kill_switch_active: bool = False
    reconciled: bool = True

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)
