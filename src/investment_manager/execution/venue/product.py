"""Product-qualified order port shared by grouped execution venue adapters."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.group.models import ExecutionLeg
from investment_manager.execution.models import Side
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import InstrumentId


class ProductOrderStatus(StrEnum):
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def terminal(self) -> bool:
        return self in {self.FILLED, self.CANCELED, self.REJECTED, self.EXPIRED}


class ProductOrder(FrozenModel):
    client_order_id: str = Field(min_length=1, max_length=36)
    venue_order_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    execution_leg_id: str = Field(min_length=1)
    instrument: InstrumentId
    side: Side
    requested_quantity: PositiveDecimal
    status: ProductOrderStatus
    filled_quantity: Decimal = Field(ge=0)
    average_fill_price: PositiveDecimal | None = None
    fee: Money = Decimal("0")
    observed_at: datetime

    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def fill_must_be_consistent(self):
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("Venue order 成交数量不能超过请求数量")
        if (self.filled_quantity > 0) != (self.average_fill_price is not None):
            raise ValueError("Venue order 成交数量与均价必须同时存在")
        if self.status == ProductOrderStatus.FILLED and (
            self.filled_quantity != self.requested_quantity
        ):
            raise ValueError("FILLED Venue order 必须全部成交")
        if self.status == ProductOrderStatus.REJECTED and self.filled_quantity > 0:
            raise ValueError(f"{self.status} Venue order 不允许携带成交")
        return self


class UnknownVenueResult(RuntimeError):
    """The caller cannot infer whether a venue mutation took effect."""


class ProductOrderVenue(Protocol):
    def query(self, client_order_id: str) -> ProductOrder | None: ...

    def submit(self, leg: ExecutionLeg, *, observed_at: datetime) -> ProductOrder: ...

    def cancel(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
    ) -> ProductOrder | None: ...
