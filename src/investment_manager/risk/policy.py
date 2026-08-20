from decimal import Decimal

from pydantic import Field

from investment_manager.kernel.configuration import StrictConfig


class RiskPolicy(StrictConfig):
    version: str
    symbol_allowlist: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    maximum_market_age_seconds: int = Field(default=180, gt=0)
    maximum_account_age_seconds: int = Field(default=60, gt=0)
    maximum_risk_fraction: Decimal = Decimal("0.005")
    maximum_total_risk_fraction: Decimal = Decimal("0.02")
    maximum_position_notional: Decimal = Decimal("2000")
    maximum_daily_loss: Decimal = Decimal("200")
    maximum_drawdown_fraction: Decimal = Decimal("0.05")
    minimum_order_notional: Decimal = Decimal("10")
    quantity_step: Decimal = Decimal("0.000001")
    reservation_ttl_seconds: int = Field(default=120, gt=0)
    kill_switch: bool = False
