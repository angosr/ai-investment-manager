from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.time import (
    require_utc,
)
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
)


class AssetTarget(FrozenModel):
    symbol: str = Field(min_length=1)
    desired_quote_notional: Money
    forecast_ids: tuple[str, ...] = Field(min_length=1)
    conservative_gross_bps: Decimal
    estimated_variable_cost_bps: Money
    conservative_net_bps: Decimal
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def target_economics_and_refs_must_be_consistent(self):
        expected_net = self.conservative_gross_bps - self.estimated_variable_cost_bps
        if self.conservative_net_bps != expected_net:
            raise ValueError("AssetTarget 净收益必须等于保守毛收益减可变成本")
        if len(set(self.forecast_ids)) != len(self.forecast_ids):
            raise ValueError("AssetTarget 不能重复引用预测")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("AssetTarget 不能重复原因码")
        return self


class PortfolioTarget(FrozenModel):
    target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    as_of: datetime
    valid_until: datetime
    reference_equity: PositiveDecimal
    targets: tuple[AssetTarget, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def target_set_must_be_bounded_and_unambiguous(self):
        if self.as_of >= self.valid_until:
            raise ValueError("PortfolioTarget 必须具有未来有效期")
        symbols = tuple(item.symbol for item in self.targets)
        if tuple(sorted(set(symbols))) != symbols:
            raise ValueError("PortfolioTarget 资产必须唯一且排序")
        if sum(item.desired_quote_notional for item in self.targets) > (
            self.reference_equity
        ):
            raise ValueError("无杠杆 MVP 的目标名义金额不能超过参考权益")
        return self
