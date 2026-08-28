from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal


class CashYieldProductObservation(FrozenModel):
    """One account-eligible, read-only product fact available after all probes finish."""

    observation_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    product_id: str = Field(pattern=r"^[A-Z0-9._-]+$")
    asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    observed_at: datetime
    available_at: datetime
    annual_rate: Decimal = Field(ge=0, le=1)
    minimum_purchase_amount: Money
    left_personal_quota: Money
    can_purchase: bool
    can_redeem: bool
    sold_out: bool
    preview_amount: PositiveDecimal
    preview_daily_reward: Money
    reward_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    source_refs: tuple[str, ...] = Field(min_length=1)

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_economics_match(self):
        if self.available_at < self.observed_at:
            raise ValueError("现金收益产品 available_at 不能早于 observed_at")
        if self.reward_asset != self.asset:
            raise ValueError("现金收益产品奖励币种必须与结算资产一致")
        if self.can_purchase and (
            self.sold_out or self.left_personal_quota < self.minimum_purchase_amount
        ):
            raise ValueError("现金收益产品申购能力与售罄或额度事实冲突")
        if tuple(sorted(set(self.source_refs))) != self.source_refs:
            raise ValueError("现金收益产品来源引用必须唯一且排序")
        expected = stable_id(
            "cash_yield_product_observation",
            content_hash(self.model_dump(exclude={"observation_id"}, mode="json")),
        )
        if self.observation_id != expected:
            raise ValueError("现金收益产品观察身份与内容不一致")
        return self


def build_cash_yield_product_observation(**values: object) -> CashYieldProductObservation:
    provisional = CashYieldProductObservation.model_construct(
        observation_id="pending",
        **values,
    )
    observation_id = stable_id(
        "cash_yield_product_observation",
        content_hash(provisional.model_dump(exclude={"observation_id"}, mode="json")),
    )
    return CashYieldProductObservation(observation_id=observation_id, **values)
