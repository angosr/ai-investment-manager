from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.forecast.models import (
    BaseForecast,
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
    MockCandidateAuthorization,
    PortfolioAccountSnapshot,
    PortfolioEdgeBasis,
    PortfolioTarget,
    SleeveTarget,
    sleeve_gross_notional,
)


class PortfolioDecisionPolicy(FrozenModel):
    version: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    enabled: bool = False
    eligible_forecast_roles: tuple[ForecastRole, ...] = (ForecastRole.PROGRAM_BASE,)
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
    forecast: BaseForecast | CalibratedForecast
    mock_authorization: MockCandidateAuthorization | None = None
    refresh_target: bool = True

    @model_validator(mode="after")
    def forecast_permission_must_be_explicit(self):
        if isinstance(self.forecast, BaseForecast):
            permission = self.mock_authorization
            if permission is None or (
                permission.producer_id != self.forecast.producer_id
                or permission.producer_version != self.forecast.producer_version
                or permission.forecast_family != self.forecast.forecast_family
            ):
                raise ValueError("BaseForecast 必须精确绑定 Mock candidate authorization")
        elif self.mock_authorization is not None:
            raise ValueError("CalibratedForecast 不得伪装成 Mock hypothesis")
        return self


def remaining_forecast_gross_bps(
    forecast: BaseForecast | CalibratedForecast,
    *,
    quote_by_instrument: dict[str, ExecutableQuote],
    as_of: datetime,
) -> Decimal:
    """Return the exact unconsumed gross edge used by the capital decision."""

    if isinstance(forecast, CalibratedForecast):
        age_seconds = Decimal(
            str(max(0, (require_utc(as_of) - forecast.available_at).total_seconds()))
        )
        decay = max(
            Decimal("0"),
            Decimal("1")
            - age_seconds
            / (Decimal("2") * forecast.expected_edge_half_life_seconds),
        )
        forecast_gross_bps = forecast.conservative_gross_bps * decay
    else:
        forecast_gross_bps = forecast.raw_score
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
    return forecast_gross_bps - max(Decimal("0"), consumed_bps)


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
        decision_valid_until: datetime | None = None,
    ) -> PortfolioTarget | None:
        if not self._policy.enabled:
            return None
        as_of = require_utc(as_of)
        if decision_valid_until is not None:
            decision_valid_until = require_utc(decision_valid_until)
            if decision_valid_until <= as_of:
                raise ValueError("Portfolio 决策截止时间必须晚于 as_of")
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
            leg.instrument.key for item in sleeves for leg in item.forecast.target.legs
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
                    if item.refresh_target
                    if self._is_eligible(
                        item,
                        quote_by_instrument=quote_by_instrument,
                        as_of=as_of,
                        current_notional=current_by_sleeve[item.sleeve_id],
                    )
                ),
                key=lambda item: (
                    -self._net_edge(
                        item,
                        quote_by_instrument=quote_by_instrument,
                        as_of=as_of,
                    ),
                    0 if isinstance(item.forecast, CalibratedForecast) else 1,
                    (
                        item.forecast.dispersion_bps
                        if isinstance(item.forecast, CalibratedForecast)
                        else Decimal("0")
                    ),
                    item.sleeve_id,
                ),
            )
        )
        retained_ids = {
            item.sleeve_id
            for item in sleeves
            if not item.refresh_target and current_by_sleeve[item.sleeve_id] > 0
        }
        desired_by_sleeve = {
            sleeve_id: current_by_sleeve[sleeve_id] for sleeve_id in retained_ids
        }
        remaining_capacity = max(
            Decimal("0"),
            account.equity * self._policy.maximum_total_exposure_fraction
            - sum(desired_by_sleeve.values()),
        )
        single_sleeve_limit = (
            account.equity * self._policy.maximum_single_sleeve_fraction
        )
        selected_ids: set[str] = set()
        for item in eligible:
            desired = min(
                single_sleeve_limit,
                self._allocation_limit(item, equity=account.equity),
                remaining_capacity,
            )
            if desired <= 0:
                break
            desired_by_sleeve[item.sleeve_id] = desired
            selected_ids.add(item.sleeve_id)
            remaining_capacity -= desired
        targets = tuple(
            self._target(
                item,
                desired_notional=desired_by_sleeve.get(
                    item.sleeve_id,
                    Decimal("0"),
                ),
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
                allocation_reason=(
                    "POSITIVE_DECISION_NET_EDGE"
                    if item.sleeve_id in selected_ids
                    else "UNCHANGED_SLEEVE_WITHOUT_NEW_FORECAST"
                    if item.sleeve_id in retained_ids
                    else "CASH_SELECTED"
                ),
            )
            for item in sleeves
            if item.sleeve_id in selected_ids
            or current_by_sleeve[item.sleeve_id] > 0
        )
        frozen_desired = {
            item.sleeve_id: item.desired_gross_notional for item in targets
        }
        turnover = sum(
            abs(
                frozen_desired.get(item.sleeve_id, Decimal("0"))
                - current_by_sleeve[item.sleeve_id]
            )
            for item in sleeves
        )
        below_rebalance_minimum = (
            Decimal("0") < turnover < self._policy.minimum_rebalance_notional
        )
        if below_rebalance_minimum:
            # Preserve the current economic target exactly.  A reason code without
            # changing the target would still let Planner emit an uneconomic order.
            targets = tuple(
                self._target(
                    item,
                    desired_notional=current_by_sleeve[item.sleeve_id],
                    quote_by_instrument=quote_by_instrument,
                    as_of=as_of,
                    allocation_reason="REBALANCE_BELOW_MINIMUM_CURRENT_TARGET",
                )
                for item in sleeves
                if current_by_sleeve[item.sleeve_id] > 0
            )
        reason_codes = set()
        if not eligible and not retained_ids:
            reason_codes.add("CASH_SELECTED_NO_ELIGIBLE_FORECAST")
        if selected_ids:
            reason_codes.add("POSITIVE_DECISION_NET_EDGE_SELECTED")
        if retained_ids:
            reason_codes.add("UNCHANGED_SLEEVE_WITHOUT_NEW_FORECAST")
        if below_rebalance_minimum:
            reason_codes.add("REBALANCE_BELOW_MINIMUM")

        valid_until = as_of + timedelta(minutes=self._policy.target_validity_minutes)
        if decision_valid_until is not None:
            valid_until = min(valid_until, decision_valid_until)
        selected = tuple(item for item in eligible if item.sleeve_id in selected_ids)
        if selected:
            valid_until = min(valid_until, *(item.forecast.valid_until for item in selected))
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._policy.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of.isoformat(),
            "valid_until": valid_until.isoformat(),
            "reference_equity": account.equity,
            "account_snapshot_id": account.snapshot_id,
            "account_snapshot_hash": content_hash(account),
            "considered_forecast_ids": tuple(sorted(item.forecast.forecast_id for item in sleeves)),
            # A cash decision still consumed every considered Forecast quote.
            # Freeze the consideration set, not only instruments selected into
            # a non-zero target, so persistence can reproduce why cash won.
            "quotes": [item.model_dump(mode="json") for item in quotes],
            "sleeves": [item.model_dump(mode="json") for item in targets],
            "reason_codes": tuple(sorted(reason_codes)),
        }
        return PortfolioTarget(
            target_id=stable_id("portfolio_target", content_hash(payload)),
            **payload,
        )

    def force_cash(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        account: PortfolioAccountSnapshot,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
        reason_codes: tuple[str, ...],
    ) -> PortfolioTarget:
        """Create an exit-only economic target from an explicit holding-risk review."""

        as_of = require_utc(as_of)
        if (
            account.cycle_id != cycle_id
            or account.as_of != as_of
            or account.portfolio_id != self._policy.portfolio_id
            or account.equity <= 0
        ):
            raise ValueError("Risk exit account 与 cycle/as_of/portfolio 不一致")
        quote_by_instrument = self._quotes(quotes=quotes, as_of=as_of)
        inputs = {item.sleeve_id: item for item in sleeves}
        current = {item.sleeve_id: item for item in account.sleeves}
        if set(inputs) != set(current) or len(inputs) != len(sleeves):
            raise ValueError("Risk exit 必须精确覆盖全部当前 Sleeve")
        targets = []
        for sleeve_id, position in sorted(current.items()):
            item = inputs[sleeve_id]
            if (
                item.forecast.target != position.target
                or item.forecast.forecast_family != position.forecast_family
            ):
                raise ValueError("Risk exit Forecast 与当前 Sleeve 身份不一致")
            if not {leg.instrument.key for leg in position.target.legs}.issubset(
                quote_by_instrument
            ):
                raise ValueError("Risk exit 缺少当前 Sleeve 报价")
            targets.append(
                SleeveTarget(
                    sleeve_id=sleeve_id,
                    forecast_family=position.forecast_family,
                    forecast_target=position.target,
                    desired_gross_notional=Decimal("0"),
                    forecast_ids=(item.forecast.forecast_id,),
                    edge_basis=self._edge_basis(item),
                    decision_gross_bps=self._forecast_gross_bps(item.forecast),
                    estimated_variable_cost_bps=item.estimated_variable_cost_bps,
                    decision_net_bps=(
                        self._forecast_gross_bps(item.forecast)
                        - item.estimated_variable_cost_bps
                    ),
                    reason_codes=("PROGRAMMATIC_RISK_EXIT",),
                )
            )
        all_reasons = tuple(sorted({"PROGRAMMATIC_RISK_EXIT", *reason_codes}))
        target_instruments = {
            leg.instrument.key
            for sleeve in targets
            for leg in sleeve.forecast_target.legs
        }
        target_quotes = tuple(
            item for item in quotes if item.instrument.key in target_instruments
        )
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._policy.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of,
            "valid_until": as_of + timedelta(minutes=self._policy.target_validity_minutes),
            "reference_equity": account.equity,
            "account_snapshot_id": account.snapshot_id,
            "account_snapshot_hash": content_hash(account),
            "considered_forecast_ids": tuple(sorted(item.forecast.forecast_id for item in sleeves)),
            "quotes": target_quotes,
            "sleeves": tuple(targets),
            "reason_codes": all_reasons,
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
        current_notional: Decimal,
    ) -> bool:
        forecast = item.forecast
        if not (
            forecast.available_at <= as_of < forecast.valid_until
            and forecast.direction == DirectionalView.UP
        ):
            return False
        net_edge = self._net_edge(
            item,
            quote_by_instrument=quote_by_instrument,
            as_of=as_of,
        )
        if isinstance(forecast, CalibratedForecast):
            return bool(
                forecast.role in self._policy.eligible_forecast_roles
                and net_edge >= self._policy.minimum_conservative_net_bps
            )
        permission = item.mock_authorization
        assert permission is not None
        threshold = (
            permission.minimum_hold_net_bps
            if current_notional > 0
            else permission.minimum_entry_net_bps
        )
        return net_edge >= threshold

    @classmethod
    def _net_edge(
        cls,
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> Decimal:
        return (
            cls._remaining_gross_edge(
                item,
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
            )
            - item.estimated_variable_cost_bps
        )

    @staticmethod
    def _remaining_gross_edge(
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> Decimal:
        return remaining_forecast_gross_bps(
            item.forecast,
            quote_by_instrument=quote_by_instrument,
            as_of=as_of,
        )

    @classmethod
    def _target(
        cls,
        item: PortfolioSleeveInput,
        *,
        desired_notional: Decimal,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
        allocation_reason: str,
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
            edge_basis=cls._edge_basis(item),
            decision_gross_bps=remaining_gross,
            estimated_variable_cost_bps=item.estimated_variable_cost_bps,
            decision_net_bps=net_edge,
            reason_codes=tuple(
                sorted(
                    (
                        (
                            f"FORECAST_ROLE:{item.forecast.role.value}"
                            if isinstance(item.forecast, CalibratedForecast)
                            else "FORECAST_BASIS:MOCK_HYPOTHESIS"
                        ),
                        allocation_reason,
                    )
                )
            ),
        )

    @staticmethod
    def _allocation_limit(
        item: PortfolioSleeveInput,
        *,
        equity: Decimal,
    ) -> Decimal:
        if item.mock_authorization is None:
            return equity
        return equity * item.mock_authorization.maximum_allocation_fraction

    @staticmethod
    def _edge_basis(item: PortfolioSleeveInput) -> PortfolioEdgeBasis:
        return (
            PortfolioEdgeBasis.MOCK_HYPOTHESIS
            if isinstance(item.forecast, BaseForecast)
            else PortfolioEdgeBasis.CALIBRATED_CONSERVATIVE
        )

    @staticmethod
    def _forecast_gross_bps(
        forecast: BaseForecast | CalibratedForecast,
    ) -> Decimal:
        return (
            forecast.raw_score
            if isinstance(forecast, BaseForecast)
            else forecast.conservative_gross_bps
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
