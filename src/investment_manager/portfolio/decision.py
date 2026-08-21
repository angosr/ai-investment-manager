from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.forecast.models import (
    CalibratedForecast,
    DirectionalView,
    ExposureDirection,
    ForecastRole,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    UnitInterval,
)
from investment_manager.market.models import ExecutableQuote
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    PortfolioTarget,
    SleeveTarget,
    sleeve_gross_notional,
)


class PortfolioDecisionPolicy(FrozenModel):
    version: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    enabled: bool = False
    eligible_forecast_roles: tuple[ForecastRole, ...] = (
        ForecastRole.PROGRAM_BASE,
    )
    minimum_conservative_net_bps: Decimal = Field(default=Decimal("5"), ge=0)
    maximum_total_exposure_fraction: UnitInterval = Decimal("0.50")
    maximum_single_sleeve_fraction: UnitInterval = Decimal("0.30")
    minimum_rebalance_notional: Money = Decimal("25")
    target_validity_minutes: int = Field(default=30, ge=1, le=1_440)

    @model_validator(mode="after")
    def policy_must_be_deterministic_and_bounded(self):
        roles = tuple(sorted(set(self.eligible_forecast_roles), key=lambda item: item.value))
        if roles != self.eligible_forecast_roles:
            raise ValueError("eligible_forecast_roles 必须唯一且排序")
        if self.maximum_single_sleeve_fraction <= 0:
            raise ValueError("单 Sleeve 暴露上限必须为正数")
        return self


class PortfolioSleeveInput(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    estimated_variable_cost_bps: Money
    forecast: CalibratedForecast


class PortfolioDecisionEngine:
    """The sole economic target owner; default OFF until replay evidence promotes it."""

    def __init__(self, policy: PortfolioDecisionPolicy) -> None:
        self._policy = policy

    def decide(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        account: PortfolioAccountSnapshot,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
    ) -> PortfolioTarget | None:
        if not self._policy.enabled:
            return None
        as_of = require_utc(as_of)
        sleeve_ids = tuple(item.sleeve_id for item in sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioSleeveInput 必须按 sleeve_id 唯一且排序")
        if (
            account.cycle_id != cycle_id
            or account.as_of != as_of
            or account.portfolio_id != self._policy.portfolio_id
        ):
            raise ValueError("Portfolio account 与 cycle/as_of/portfolio 不一致")
        if account.equity <= 0:
            return None
        quote_by_instrument = self._quotes(quotes=quotes, as_of=as_of)
        required_quote_keys = {
            leg.instrument.key
            for item in sleeves
            for leg in item.forecast.target.legs
        }
        account_by_sleeve = {item.sleeve_id: item for item in account.sleeves}
        missing_positions = set(account_by_sleeve) - set(sleeve_ids)
        if missing_positions:
            raise ValueError("Portfolio 输入必须显式覆盖全部当前 Sleeve")
        if set(quote_by_instrument) != required_quote_keys:
            raise ValueError("ExecutableQuote 必须精确覆盖 Portfolio Sleeve Instruments")
        current_by_sleeve = {
            item.sleeve_id: sleeve_gross_notional(
                account_by_sleeve.get(item.sleeve_id),
                quote_by_instrument=quote_by_instrument,
            )
            for item in sleeves
        }
        for item in sleeves:
            expected_id = SleeveTarget.identity_for(
                portfolio_id=self._policy.portfolio_id,
                forecast_family=item.forecast.forecast_family,
                forecast_target_id=item.forecast.target.target_id,
            )
            if item.sleeve_id != expected_id:
                raise ValueError("PortfolioSleeveInput sleeve_id 与 Forecast 不一致")
            missing = set(leg.instrument.key for leg in item.forecast.target.legs) - set(
                quote_by_instrument
            )
            if missing:
                raise ValueError("PortfolioSleeveInput 缺少产品级可成交报价")

        eligible = tuple(
            sorted(
                (
                    item
                    for item in sleeves
                    if self._is_eligible(
                        item,
                        quote_by_instrument=quote_by_instrument,
                        as_of=as_of,
                    )
                ),
                key=lambda item: (
                    -self._net_edge(
                        item,
                        quote_by_instrument=quote_by_instrument,
                        as_of=as_of,
                    ),
                    item.forecast.dispersion_bps,
                    item.sleeve_id,
                ),
            )[:1]
        )
        selected_ids = {item.sleeve_id for item in eligible}
        desired_notional = (
            min(
                account.equity * self._policy.maximum_total_exposure_fraction,
                account.equity * self._policy.maximum_single_sleeve_fraction,
            )
            if eligible
            else Decimal("0")
        )
        targets = tuple(
            self._target(
                item,
                desired_notional=(
                    desired_notional if item.sleeve_id in selected_ids else Decimal("0")
                ),
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
            )
            for item in sleeves
            if item.sleeve_id in selected_ids or current_by_sleeve[item.sleeve_id] > 0
        )
        desired_by_sleeve = {
            item.sleeve_id: item.desired_gross_notional for item in targets
        }
        turnover = sum(
            abs(
                desired_by_sleeve.get(item.sleeve_id, Decimal("0"))
                - current_by_sleeve[item.sleeve_id]
            )
            for item in sleeves
        )
        reason_codes = set()
        if not eligible:
            reason_codes.add("CASH_SELECTED_NO_ELIGIBLE_FORECAST")
        else:
            reason_codes.add("POSITIVE_CONSERVATIVE_NET_EDGE_SELECTED")
        if turnover < self._policy.minimum_rebalance_notional:
            reason_codes.add("REBALANCE_BELOW_MINIMUM")

        valid_until = as_of + timedelta(minutes=self._policy.target_validity_minutes)
        if eligible:
            valid_until = min(valid_until, *(item.forecast.valid_until for item in eligible))
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._policy.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of.isoformat(),
            "valid_until": valid_until.isoformat(),
            "reference_equity": account.equity,
            "account_snapshot_id": account.snapshot_id,
            "account_snapshot_hash": content_hash(account),
            "considered_forecast_ids": tuple(
                sorted(item.forecast.forecast_id for item in sleeves)
            ),
            "quotes": [item.model_dump(mode="json") for item in quotes],
            "sleeves": [item.model_dump(mode="json") for item in targets],
            "reason_codes": tuple(sorted(reason_codes)),
        }
        return PortfolioTarget(
            target_id=stable_id("portfolio_target", content_hash(payload)),
            **payload,
        )

    def _is_eligible(
        self,
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> bool:
        forecast = item.forecast
        return bool(
            forecast.available_at <= as_of < forecast.valid_until
            and forecast.direction == DirectionalView.UP
            and forecast.role in self._policy.eligible_forecast_roles
            and self._net_edge(
                item,
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
            )
            >= self._policy.minimum_conservative_net_bps
        )

    @classmethod
    def _net_edge(
        cls,
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> Decimal:
        return cls._remaining_gross_edge(
            item,
            quote_by_instrument=quote_by_instrument,
            as_of=as_of,
        ) - item.estimated_variable_cost_bps

    @staticmethod
    def _remaining_gross_edge(
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> Decimal:
        forecast = item.forecast
        age_seconds = Decimal(
            str(max(0, (as_of - forecast.available_at).total_seconds()))
        )
        decay = max(
            Decimal("0"),
            Decimal("1")
            - age_seconds
            / (Decimal("2") * forecast.expected_edge_half_life_seconds),
        )
        references = {item.instrument_id: item.price for item in forecast.reference_prices}
        consumed_bps = Decimal("0")
        for leg in forecast.target.legs:
            quote = quote_by_instrument[leg.instrument.key]
            exit_price = quote.bid if leg.direction == ExposureDirection.LONG else quote.ask
            sign = Decimal("1") if leg.direction == ExposureDirection.LONG else Decimal("-1")
            consumed_bps += (
                sign
                * leg.gross_weight
                * (exit_price / references[leg.instrument.key] - Decimal("1"))
                * Decimal("10000")
            )
        return forecast.conservative_gross_bps * decay - max(
            Decimal("0"), consumed_bps
        )

    @classmethod
    def _target(
        cls,
        item: PortfolioSleeveInput,
        *,
        desired_notional: Decimal,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> SleeveTarget:
        remaining_gross = cls._remaining_gross_edge(
            item,
            quote_by_instrument=quote_by_instrument,
            as_of=as_of,
        )
        net_edge = remaining_gross - item.estimated_variable_cost_bps
        return SleeveTarget(
            sleeve_id=item.sleeve_id,
            forecast_family=item.forecast.forecast_family,
            forecast_target=item.forecast.target,
            desired_gross_notional=desired_notional,
            forecast_ids=(item.forecast.forecast_id,),
            conservative_gross_bps=remaining_gross,
            estimated_variable_cost_bps=item.estimated_variable_cost_bps,
            conservative_net_bps=net_edge,
            reason_codes=tuple(
                sorted(
                    (
                        f"FORECAST_ROLE:{item.forecast.role.value}",
                        (
                            "POSITIVE_CONSERVATIVE_NET_EDGE"
                            if desired_notional > 0
                            else "CASH_SELECTED"
                        ),
                    )
                )
            ),
        )

    @staticmethod
    def _quotes(
        *,
        quotes: tuple[ExecutableQuote, ...],
        as_of: datetime,
    ) -> dict[str, ExecutableQuote]:
        keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ExecutableQuote 必须按 Instrument 唯一且排序")
        if any(item.as_of != as_of for item in quotes):
            raise ValueError("ExecutableQuote 必须冻结在 Portfolio as_of")
        return {item.instrument.key: item for item in quotes}
