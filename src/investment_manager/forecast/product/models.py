"""Deterministic projection from one economic forecast into product payoffs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.contracts import ForecastContract, ForecastPriceAnchor
from investment_manager.forecast.models import ExposureDirection, ForecastTarget
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastLegOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, UnitInterval
from investment_manager.market.models import InstrumentId, InstrumentProduct

_BPS = Decimal("10000")


class ProductProjectionState(FrozenModel):
    """Point-in-time assumptions required to express one economic view in a product."""

    target: ForecastTarget
    entry_anchor: ForecastPriceAnchor
    valid_until: datetime
    expected_exit_basis_bps: Decimal
    expected_funding_bps: Decimal
    mapping_uncertainty_bps: Decimal = Field(ge=0)
    initial_margin_fraction: UnitInterval
    product_rule_refs: tuple[str, ...] = Field(min_length=1)
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_state_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def state_must_describe_one_complete_product(self):
        if len(self.target.legs) != 1:
            raise ValueError("Product payoff projection 当前只接受单腿产品")
        leg = self.target.legs[0]
        if self.entry_anchor.instrument_id != leg.instrument.key:
            raise ValueError("Product projection entry anchor 与目标产品不一致")
        if self.valid_until <= self.entry_anchor.available_at:
            raise ValueError("Product projection 状态形成时已失效")
        if tuple(sorted(set(self.product_rule_refs))) != self.product_rule_refs:
            raise ValueError("Product projection rule refs 必须唯一且排序")
        if tuple(sorted(set(self.input_refs))) != self.input_refs:
            raise ValueError("Product projection input refs 必须唯一且排序")
        if leg.instrument.product == InstrumentProduct.SPOT:
            if self.expected_exit_basis_bps != 0 or self.expected_funding_bps != 0:
                raise ValueError("Spot projection 不得伪造 basis 或 funding")
            if self.initial_margin_fraction != Decimal("1"):
                raise ValueError("Spot projection 的资金占用比例必须为 1")
        elif self.mapping_uncertainty_bps <= 0:
            raise ValueError("Derivative projection 必须显式量化映射不确定性")
        return self


class ProductPayoffBucket(FrozenModel):
    source_bucket_id: str = Field(min_length=1)
    probability: Decimal = Field(ge=0, le=1)
    payoff_bps: Decimal
    conservative_payoff_bps: Decimal

    @model_validator(mode="after")
    def conservative_payoff_cannot_exceed_point_estimate(self):
        if self.conservative_payoff_bps > self.payoff_bps:
            raise ValueError("Product payoff 保守情景不能优于点估计")
        return self


class ProductPayoffProjection(FrozenModel):
    """One immutable, non-AI product expression of a source economic forecast."""

    projection_id: str = Field(min_length=1)
    projection_version: str = Field(min_length=1)
    economic_exposure_id: str = Field(min_length=1)
    source_forecast_id: str = Field(min_length=1)
    source_contract_id: str = Field(min_length=1)
    reference_instrument: InstrumentId
    target: ForecastTarget
    projected_at: datetime
    source_entry_valid_until: datetime
    valid_until: datetime
    evaluation_at: datetime
    entry_anchor: ForecastPriceAnchor
    entry_basis_bps: Decimal
    expected_exit_basis_bps: Decimal
    expected_funding_bps: Decimal
    mapping_uncertainty_bps: Decimal = Field(ge=0)
    initial_margin_fraction: UnitInterval
    outcome_payoffs: tuple[ProductPayoffBucket, ...] = Field(min_length=3)
    expected_gross_bps: Decimal
    conservative_gross_bps: Decimal
    product_rule_refs: tuple[str, ...] = Field(min_length=1)
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_projected_at = field_validator("projected_at")(require_utc)
    _utc_source_entry_valid_until = field_validator("source_entry_valid_until")(
        require_utc
    )
    _utc_valid_until = field_validator("valid_until")(require_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)

    @model_validator(mode="after")
    def identity_distribution_and_economics_must_reconcile(self):
        if not self.projected_at < self.valid_until <= self.evaluation_at:
            raise ValueError("Product projection 形成、入场窗和结算时间非法")
        if self.source_entry_valid_until > self.evaluation_at:
            raise ValueError("Product projection 源入场窗不能晚于经济结算")
        if len(self.target.legs) != 1:
            raise ValueError("Product payoff projection 当前只接受单腿产品")
        leg = self.target.legs[0]
        if (
            leg.instrument.base_asset != self.reference_instrument.base_asset
            or leg.instrument.quote_asset != self.reference_instrument.quote_asset
            or leg.instrument.settlement_asset != self.reference_instrument.settlement_asset
        ):
            raise ValueError("Product projection 与规范参考不是同一线性经济暴露")
        if self.entry_anchor.instrument_id != leg.instrument.key:
            raise ValueError("Product projection entry anchor 与目标产品不一致")
        bucket_ids = tuple(item.source_bucket_id for item in self.outcome_payoffs)
        if len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("Product projection 不能重复源 bucket")
        if sum((item.probability for item in self.outcome_payoffs), Decimal("0")) != 1:
            raise ValueError("Product projection 概率之和必须为 1")
        expected = sum(
            (item.probability * item.payoff_bps for item in self.outcome_payoffs),
            Decimal("0"),
        )
        conservative = sum(
            (
                item.probability * item.conservative_payoff_bps
                for item in self.outcome_payoffs
            ),
            Decimal("0"),
        )
        if self.expected_gross_bps != expected:
            raise ValueError("Product projection 期望收益与 payoff 分布不一致")
        if any(
            item.conservative_payoff_bps
            != item.payoff_bps - self.mapping_uncertainty_bps
            for item in self.outcome_payoffs
        ):
            raise ValueError("Product projection 映射不确定性未进入各情景保守包络")
        if self.conservative_gross_bps != conservative:
            raise ValueError("Product projection 保守收益与情景包络不一致")
        for values, label in (
            (self.product_rule_refs, "rule refs"),
            (self.input_refs, "input refs"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"Product projection {label} 必须唯一且排序")
        expected_id = stable_id(
            "product_payoff_projection",
            content_hash(self.model_dump(mode="json", exclude={"projection_id"})),
        )
        if self.projection_id != expected_id:
            raise ValueError("Product projection ID 与冻结内容不一致")
        return self


class ProductPayoffOutcome(FrozenModel):
    """Realized executable payoff for one immutable product projection."""

    outcome_id: str = Field(min_length=1)
    projection_id: str = Field(min_length=1)
    source_forecast_id: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    status: ForecastOutcomeStatus
    projected_at: datetime
    evaluation_at: datetime
    settled_at: datetime
    leg: ForecastLegOutcome | None = None
    realized_gross_bps: Decimal | None = None
    reason_code: str = Field(min_length=1)

    _utc_outcome_projected_at = field_validator("projected_at")(require_utc)
    _utc_outcome_evaluation_at = field_validator("evaluation_at")(require_utc)
    _utc_outcome_settled_at = field_validator("settled_at")(require_utc)

    @model_validator(mode="after")
    def identity_timing_and_result_must_be_consistent(self):
        if not self.projected_at < self.evaluation_at <= self.settled_at:
            raise ValueError("Product payoff outcome 时间顺序非法")
        settled = self.status == ForecastOutcomeStatus.SETTLED
        if settled != (self.leg is not None and self.realized_gross_bps is not None):
            raise ValueError("Product payoff outcome 状态与收益不一致")
        if self.leg is not None and self.realized_gross_bps != (
            self.leg.price_return_bps + self.leg.funding_return_bps
        ):
            raise ValueError("Product payoff outcome 总收益与 Leg 不一致")
        expected_id = stable_id(
            "product_payoff_outcome",
            self.projection_id,
            self.evaluation_version,
        )
        if self.outcome_id != expected_id:
            raise ValueError("Product payoff outcome ID 不一致")
        return self


def project_product_payoff(
    *,
    contract: ForecastContract,
    forecast: BaseForecast,
    state: ProductProjectionState,
    economic_exposure_id: str,
    projection_version: str,
) -> ProductPayoffProjection:
    """Project a single reference-price distribution without another model call."""

    if forecast.contract_id != contract.contract_id or forecast.target != contract.target:
        raise ValueError("Product projection 的 Forecast 与 Contract 不一致")
    if len(contract.target.legs) != 1:
        raise ValueError("Product projection 的规范参考必须是单腿经济暴露")
    reference_leg = contract.target.legs[0]
    if reference_leg.direction != ExposureDirection.LONG:
        raise ValueError("Product projection 的规范参考必须使用正向价格方向")
    reference = reference_leg.instrument
    product_leg = state.target.legs[0]
    if (
        product_leg.instrument.base_asset != reference.base_asset
        or product_leg.instrument.quote_asset != reference.quote_asset
        or product_leg.instrument.settlement_asset != reference.settlement_asset
    ):
        raise ValueError("Product projection 只能映射同一基础、报价和结算资产")
    reference_anchor = next(
        (item for item in forecast.cutoff_prices if item.instrument_id == reference.key),
        None,
    )
    if reference_anchor is None:
        raise ValueError("Product projection 缺少规范参考 cutoff anchor")
    if state.entry_anchor.available_at < forecast.available_at:
        raise ValueError("Product projection 不得使用 Forecast 完成前的产品入场锚点")
    projected_at = max(forecast.available_at, state.entry_anchor.available_at)
    if projected_at >= forecast.economic_horizon_end:
        raise ValueError("Product projection 形成时经济 Forecast 已结算")

    direction = (
        Decimal("1")
        if product_leg.direction == ExposureDirection.LONG
        else Decimal("-1")
    )
    entry_basis_bps = (
        state.entry_anchor.price / reference_anchor.price - Decimal("1")
    ) * _BPS
    payoffs = []
    for probability, bucket in zip(
        forecast.outcome_probabilities,
        contract.outcome_buckets,
        strict=True,
    ):
        if probability.bucket_id != bucket.bucket_id:
            raise ValueError("Product projection 的 Forecast bucket 与 Contract 不一致")
        reference_factor = Decimal("1") + bucket.representative_bps / _BPS
        exit_basis_factor = Decimal("1") + state.expected_exit_basis_bps / _BPS
        if reference_factor <= 0 or exit_basis_factor <= 0:
            raise ValueError("Product projection 的价格映射产生非正价格")
        future_product_price = (
            reference_anchor.price * reference_factor * exit_basis_factor
        )
        price_payoff_bps = direction * (
            future_product_price / state.entry_anchor.price - Decimal("1")
        ) * _BPS
        funding_payoff_bps = -direction * state.expected_funding_bps
        payoffs.append(
            ProductPayoffBucket(
                source_bucket_id=bucket.bucket_id,
                probability=probability.probability,
                payoff_bps=price_payoff_bps + funding_payoff_bps,
                conservative_payoff_bps=(
                    price_payoff_bps
                    + funding_payoff_bps
                    - state.mapping_uncertainty_bps
                ),
            )
        )
    expected = sum(
        (item.probability * item.payoff_bps for item in payoffs),
        Decimal("0"),
    )
    values = {
        "projection_version": projection_version,
        "economic_exposure_id": economic_exposure_id,
        "source_forecast_id": forecast.forecast_id,
        "source_contract_id": contract.contract_id,
        "reference_instrument": reference,
        "target": state.target,
        "projected_at": projected_at,
        "source_entry_valid_until": forecast.valid_until,
        "valid_until": min(state.valid_until, forecast.economic_horizon_end),
        "evaluation_at": forecast.economic_horizon_end,
        "entry_anchor": state.entry_anchor,
        "entry_basis_bps": entry_basis_bps,
        "expected_exit_basis_bps": state.expected_exit_basis_bps,
        "expected_funding_bps": state.expected_funding_bps,
        "mapping_uncertainty_bps": state.mapping_uncertainty_bps,
        "initial_margin_fraction": state.initial_margin_fraction,
        "outcome_payoffs": tuple(payoffs),
        "expected_gross_bps": expected,
        "conservative_gross_bps": sum(
            (
                item.probability * item.conservative_payoff_bps
                for item in payoffs
            ),
            Decimal("0"),
        ),
        "product_rule_refs": state.product_rule_refs,
        "input_refs": tuple(
            sorted(
                {
                    forecast.forecast_id,
                    reference_anchor.quote_ref,
                    state.entry_anchor.quote_ref,
                    *state.input_refs,
                    *state.product_rule_refs,
                }
            )
        ),
    }
    return ProductPayoffProjection(
        projection_id=stable_id(
            "product_payoff_projection",
            content_hash(values),
        ),
        **values,
    )


__all__ = [
    "ProductPayoffBucket",
    "ProductPayoffOutcome",
    "ProductPayoffProjection",
    "ProductProjectionState",
    "project_product_payoff",
]
