from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
    UnitInterval,
)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


SUPPORTED_OPEN_SIDES: tuple[Side, ...] = (Side.BUY,)


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    RISK_ACCEPTED = "RISK_ACCEPTED"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class PositionLifecycleStatus(StrEnum):
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED = "PROTECTED"
    PROTECTION_FAILED = "PROTECTION_FAILED"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    PROGRAM_SIGNAL = "PROGRAM_SIGNAL"
    MAX_HOLDING_TIME = "MAX_HOLDING_TIME"
    PROTECTION_FAILURE = "PROTECTION_FAILURE"


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


class ProgramExitCondition(FrozenModel):
    """Frozen deterministic de-risking rule carried by the position."""

    version: str
    condition_type: Literal["CLOSE_BELOW_MOVING_AVERAGE"] = (
        "CLOSE_BELOW_MOVING_AVERAGE"
    )
    bar_interval_minutes: int = Field(gt=0)
    moving_average_bars: int = Field(ge=2)


class Fill(FrozenModel):
    fill_id: str
    order_id: str
    event_time: datetime
    price: PositiveDecimal
    quantity: PositiveDecimal
    fee: Money

    _utc_event_time = field_validator("event_time")(require_utc)


class Order(FrozenModel):
    order_id: str
    client_order_id: str
    cycle_id: str
    intent_id: str
    symbol: str
    side: Side
    order_type: OrderType
    requested_quantity: PositiveDecimal
    limit_price: PositiveDecimal | None = None
    status: OrderStatus
    fills: tuple[Fill, ...] = ()


class PositionLifecycle(FrozenModel):
    position_id: str
    cycle_id: str
    intent_id: str
    entry_order_id: str
    reservation_id: str
    symbol: str
    quantity: PositiveDecimal
    entry_price: PositiveDecimal
    entry_fee: Money
    stop_price: PositiveDecimal
    opened_at: datetime
    max_exit_at: datetime
    highest_price: PositiveDecimal
    lowest_price: PositiveDecimal
    status: PositionLifecycleStatus
    protection_id: str | None = None
    closed_at: datetime | None = None
    exit_order_id: str | None = None
    exit_reason: ExitReason | None = None
    program_exit: ProgramExitCondition | None = None

    _utc_opened_at = field_validator("opened_at")(require_utc)
    _utc_max_exit_at = field_validator("max_exit_at")(require_utc)
    _utc_closed_at = field_validator("closed_at")(optional_utc)
