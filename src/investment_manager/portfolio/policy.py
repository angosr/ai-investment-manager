from decimal import Decimal

from pydantic import Field

from investment_manager.kernel.configuration import StrictConfig


class CompositionPolicy(StrictConfig):
    version: str


class FrequencyPolicy(StrictConfig):
    version: str
    cooldown_minutes: int = Field(default=30, ge=0)
    maximum_orders_per_day: int = Field(default=8, gt=0)
    minimum_net_edge_bps: Decimal = Field(default=Decimal("4"), ge=0)
    funding_bps: Decimal = Decimal("0")
    latency_bps: Decimal = Field(default=Decimal("0.5"), ge=0)
    adverse_selection_bps: Decimal = Field(default=Decimal("0.75"), ge=0)
    uncertainty_buffer_bps: Decimal = Field(default=Decimal("1.5"), ge=0)
