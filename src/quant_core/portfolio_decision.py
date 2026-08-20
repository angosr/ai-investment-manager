from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, model_validator

from quant_core.asset_management import (
    AssetTarget,
    CalibratedForecast,
    ForecastRole,
    PortfolioTarget,
)
from quant_core.domain import (
    DirectionalView,
    FrozenModel,
    Money,
    PositiveDecimal,
    UnitInterval,
    _require_utc,
)
from quant_core.ids import content_hash, stable_id


class PortfolioDecisionPolicy(FrozenModel):
    version: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    enabled: bool = False
    eligible_forecast_roles: tuple[ForecastRole, ...] = (
        ForecastRole.PROGRAM_BASE,
    )
    minimum_conservative_net_bps: Decimal = Field(default=Decimal("5"), ge=0)
    maximum_total_exposure_fraction: UnitInterval = Decimal("0.50")
    maximum_single_asset_fraction: UnitInterval = Decimal("0.30")
    minimum_rebalance_notional: Money = Decimal("25")
    target_validity_minutes: int = Field(default=30, ge=1, le=1_440)

    @model_validator(mode="after")
    def policy_must_be_deterministic_and_bounded(self):
        roles = tuple(sorted(set(self.eligible_forecast_roles), key=lambda item: item.value))
        if roles != self.eligible_forecast_roles:
            raise ValueError("eligible_forecast_roles 必须唯一且排序")
        if self.maximum_single_asset_fraction <= 0:
            raise ValueError("单资产暴露上限必须为正数")
        return self


class PortfolioAssetInput(FrozenModel):
    symbol: str = Field(min_length=1)
    current_quote_notional: Money
    current_price: PositiveDecimal
    estimated_variable_cost_bps: Money
    forecast: CalibratedForecast | None = None

    @model_validator(mode="after")
    def forecast_symbol_must_match(self):
        if self.forecast is not None and self.forecast.symbol != self.symbol:
            raise ValueError("PortfolioAssetInput 与 Forecast symbol 不一致")
        return self


class PortfolioDecisionEngine:
    """The sole economic target owner; default OFF until replay evidence promotes it."""

    def __init__(self, policy: PortfolioDecisionPolicy) -> None:
        self._policy = policy

    def decide(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        reference_equity: PositiveDecimal,
        assets: tuple[PortfolioAssetInput, ...],
    ) -> PortfolioTarget | None:
        if not self._policy.enabled:
            return None
        as_of = _require_utc(as_of)
        symbols = tuple(item.symbol for item in assets)
        if tuple(sorted(set(symbols))) != symbols:
            raise ValueError("PortfolioAssetInput 必须按 symbol 唯一且排序")

        eligible = tuple(
            sorted(
                (
                    item
                    for item in assets
                    if self._is_eligible(item, as_of=as_of)
                ),
                key=lambda item: (
                    -self._net_edge(item, as_of=as_of),
                    item.forecast.dispersion_bps,
                    item.symbol,
                ),
            )[:1]
        )
        total_capacity = reference_equity * (
            self._policy.maximum_total_exposure_fraction
        )
        per_asset_capacity = reference_equity * (
            self._policy.maximum_single_asset_fraction
        )
        desired_notional = (
            min(total_capacity, per_asset_capacity)
            if eligible
            else Decimal("0")
        )
        targets = tuple(
            sorted(
                (
                    self._target(
                        item,
                        desired_notional=desired_notional,
                        as_of=as_of,
                    )
                    for item in eligible
                ),
                key=lambda item: item.symbol,
            )
        )
        desired_by_symbol = {
            item.symbol: item.desired_quote_notional for item in targets
        }
        turnover = sum(
            abs(
                desired_by_symbol.get(item.symbol, Decimal("0"))
                - item.current_quote_notional
            )
            for item in assets
        )
        if turnover < self._policy.minimum_rebalance_notional:
            return None

        valid_until = as_of + timedelta(
            minutes=self._policy.target_validity_minutes
        )
        if eligible:
            valid_until = min(
                valid_until,
                *(item.forecast.valid_until for item in eligible),
            )
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._policy.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of.isoformat(),
            "valid_until": valid_until.isoformat(),
            "reference_equity": reference_equity,
            "targets": [item.model_dump(mode="json") for item in targets],
        }
        return PortfolioTarget(
            target_id=stable_id("portfolio_target", content_hash(payload)),
            **payload,
        )

    def _is_eligible(
        self,
        item: PortfolioAssetInput,
        *,
        as_of: datetime,
    ) -> bool:
        forecast = item.forecast
        return bool(
            forecast is not None
            and forecast.available_at <= as_of < forecast.valid_until
            and forecast.direction == DirectionalView.UP
            and forecast.role in self._policy.eligible_forecast_roles
            and self._net_edge(item, as_of=as_of)
            >= self._policy.minimum_conservative_net_bps
        )

    @classmethod
    def _net_edge(
        cls,
        item: PortfolioAssetInput,
        *,
        as_of: datetime,
    ) -> Decimal:
        return cls._remaining_gross_edge(item, as_of=as_of) - (
            item.estimated_variable_cost_bps
        )

    @staticmethod
    def _remaining_gross_edge(
        item: PortfolioAssetInput,
        *,
        as_of: datetime,
    ) -> Decimal:
        assert item.forecast is not None
        age_seconds = Decimal(
            str(max(0, (as_of - item.forecast.available_at).total_seconds()))
        )
        decay = max(
            Decimal("0"),
            Decimal("1")
            - age_seconds
            / (Decimal("2") * item.forecast.expected_edge_half_life_seconds),
        )
        consumed_bps = (
            max(
                Decimal("0"),
                (
                    item.current_price / item.forecast.reference_price
                    - Decimal("1")
                )
                * Decimal("10000"),
            )
            if item.forecast.direction == DirectionalView.UP
            else Decimal("0")
        )
        return item.forecast.conservative_gross_bps * decay - consumed_bps

    @classmethod
    def _target(
        cls,
        item: PortfolioAssetInput,
        *,
        desired_notional: Decimal,
        as_of: datetime,
    ) -> AssetTarget:
        assert item.forecast is not None
        remaining_gross = cls._remaining_gross_edge(item, as_of=as_of)
        net_edge = cls._net_edge(item, as_of=as_of)
        return AssetTarget(
            symbol=item.symbol,
            desired_quote_notional=desired_notional,
            forecast_ids=(item.forecast.forecast_id,),
            conservative_gross_bps=remaining_gross,
            estimated_variable_cost_bps=item.estimated_variable_cost_bps,
            conservative_net_bps=net_edge,
            reason_codes=(
                f"FORECAST_ROLE:{item.forecast.role.value}",
                "POSITIVE_CONSERVATIVE_NET_EDGE",
            ),
        )
