"""Point-in-time, cost-after capital comparison across Forecast producers."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from investment_manager.forecast.contracts import ForecastSlotStratum
from investment_manager.forecast.product.models import ProductPayoffProjection
from investment_manager.forecast.results import BaseForecast
from investment_manager.governance.evaluation.logical_account import (
    LOGICAL_ACCOUNT_EVALUATION_VERSION,
    LogicalAccountPath,
    LogicalAccountStep,
    ProducerDecisionPanel,
    ProducerLogicalAccount,
    ProducerPanelLedger,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.features import point_in_time_quote_views
from investment_manager.market.models import ExecutableQuote, InstrumentId, InstrumentProduct
from investment_manager.market.perpetual.models import FundingSettlement
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.decision import PortfolioSleeveInput
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    PortfolioAccountSnapshot,
    SleevePosition,
    SleeveTarget,
)
from investment_manager.portfolio.policy import CapitalPolicy, SleeveRiskTemplate
from investment_manager.risk.portfolio import SleeveRiskProfile


class ProducerCapitalPathEvidence(FrozenModel):
    """One producer's complete, independently advanced capital path."""

    label: str
    producer_id: str
    producer_behavior_id: str
    panel_ids: tuple[str, ...]
    steps: tuple[LogicalAccountStep, ...]
    path: LogicalAccountPath


class ProducerCapitalComparisonEvidence(FrozenModel):
    """Cost-after comparison on the exact DecisionSlot panels shared by all paths."""

    comparison_id: str
    evaluation_version: str
    as_of: datetime
    initial_cash: Decimal
    included_strata: tuple[ForecastSlotStratum, ...]
    shared_decision_slot_sets: tuple[tuple[str, ...], ...]
    paths: tuple[ProducerCapitalPathEvidence, ...]


class ProductPayoffBuilder(Protocol):
    def build(
        self,
        forecast: BaseForecast,
        *,
        as_of: datetime,
    ) -> tuple[ProductPayoffProjection, ...]: ...


class ProducerCapitalReplay:
    """Translate complete producer panels into one independent cost-after path."""

    def __init__(
        self,
        *,
        producer_behavior_id: str,
        capital_policy: CapitalPolicy,
        initial_cash: Decimal,
        market: MarketDataStore,
        product_payoffs_by_family: Mapping[str, ProductPayoffBuilder],
        sleeve_risk: SleeveRiskTemplate,
    ) -> None:
        self._behavior_id = producer_behavior_id
        self._policy = capital_policy
        self._market = market
        self._product_payoffs = dict(product_payoffs_by_family)
        self._sleeve_risk = sleeve_risk
        self._account = ProducerLogicalAccount(
            producer_behavior_id=producer_behavior_id,
            capital_policy=capital_policy,
            initial_cash=initial_cash,
        )
        self._support_by_sleeve: dict[str, PortfolioSleeveInput] = {}
        self._funding_start: datetime | None = None

    @property
    def account(self) -> ProducerLogicalAccount:
        return self._account

    @property
    def producer_behavior_id(self) -> str:
        return self._behavior_id

    def advance(self, panel: ProducerDecisionPanel) -> LogicalAccountStep:
        if panel.producer_behavior_id != self._behavior_id:
            raise ValueError("Producer panel 与逻辑账户行为身份不一致")
        as_of = panel.available_at
        current = self._account.current_account
        fresh: dict[str, PortfolioSleeveInput] = {}
        for forecast in panel.forecasts:
            projector = self._projector(forecast.outcome_family_id)
            projections = projector.build(forecast, as_of=as_of)
            if not projections and not self._holds_family(current, forecast.outcome_family_id):
                raise PointInTimeInputUnavailable(
                    f"{forecast.outcome_family_id} Forecast 没有可执行产品收益投影"
                )
            authorization = self._authorization(forecast)
            for projection in projections:
                sleeve = PortfolioSleeveInput(
                    sleeve_id=SleeveTarget.identity_for(
                        portfolio_id=self._policy.decision.portfolio_id,
                        forecast_family=forecast.outcome_family_id,
                        forecast_target_id=projection.target.target_id,
                    ),
                    forecast=forecast,
                    payoff_projection=projection,
                    capital_authorization=authorization,
                )
                fresh[sleeve.sleeve_id] = sleeve
                self._support_by_sleeve[sleeve.sleeve_id] = sleeve
        for position in () if current is None else current.sleeves:
            if position.sleeve_id in fresh:
                continue
            held = self._held_support(position, as_of=as_of)
            fresh[held.sleeve_id] = held
            self._support_by_sleeve[held.sleeve_id] = held
        sleeves = tuple(fresh[key] for key in sorted(fresh))
        quotes = self._quotes(sleeves=sleeves, as_of=as_of)
        return self._account.advance(
            as_of=as_of,
            sleeves=sleeves,
            quotes=quotes,
            risk_profiles=tuple(self._risk_profile(item) for item in sleeves),
            funding_settlements=self._funding(as_of=as_of),
        )

    def mark(self, *, as_of: datetime) -> PortfolioAccountSnapshot:
        current = self._account.current_account
        if current is None:
            raise ValueError("Producer capital 尚不能在首个 panel 前估值")
        instruments = {item.instrument.key: item.instrument for item in current.positions}
        return self._account.mark(
            as_of=as_of,
            quotes=self._instrument_quotes(instruments=instruments, as_of=as_of),
            funding_settlements=self._funding(as_of=as_of),
        )

    def _held_support(
        self,
        position: SleevePosition,
        *,
        as_of: datetime,
    ) -> PortfolioSleeveInput:
        original = self._support_by_sleeve.get(position.sleeve_id)
        if original is None or not isinstance(original.forecast, BaseForecast):
            raise ValueError("逻辑账户持仓缺少自身原始 Forecast 支撑")
        current_projection = None
        if as_of < original.forecast.economic_horizon_end:
            try:
                current_projection = next(
                    (
                        item
                        for item in self._projector(position.forecast_family).build(
                            original.forecast,
                            as_of=as_of,
                        )
                        if item.target == position.target
                    ),
                    None,
                )
            except PointInTimeInputUnavailable:
                current_projection = None
        projection = current_projection or original.payoff_projection
        if projection is None:
            raise ValueError("逻辑账户持仓缺少原始产品收益投影")
        return PortfolioSleeveInput(
            sleeve_id=position.sleeve_id,
            forecast=original.forecast,
            payoff_projection=projection,
            payoff_projection_current=current_projection is not None,
            capital_authorization=None,
            new_capital_allowed=False,
        )

    def _quotes(
        self,
        *,
        sleeves: tuple[PortfolioSleeveInput, ...],
        as_of: datetime,
    ) -> tuple[ExecutableQuote, ...]:
        return self._instrument_quotes(
            instruments={
                leg.instrument.key: leg.instrument
                for sleeve in sleeves
                for leg in sleeve.target.legs
            },
            as_of=as_of,
        )

    def _instrument_quotes(
        self,
        *,
        instruments: Mapping[str, InstrumentId],
        as_of: datetime,
    ) -> tuple[ExecutableQuote, ...]:
        schedule = (
            self._market.latest_trading_schedule(as_of=as_of)
            if any(
                item.product == InstrumentProduct.TRADFI_PERPETUAL for item in instruments.values()
            )
            else None
        )
        quotes: list[ExecutableQuote] = []
        for key in sorted(instruments):
            instrument = instruments[key]
            views = point_in_time_quote_views(
                market=self._market,
                instrument=instrument,
                as_of=as_of,
                maximum_live_age_seconds=self._policy.risk.maximum_quote_age_seconds,
                trading_schedule=(
                    schedule if instrument.product == InstrumentProduct.TRADFI_PERPETUAL else None
                ),
            )
            if views is None or views[1] is None:
                raise PointInTimeInputUnavailable(f"逻辑账户缺少 {key} 可执行报价")
            quotes.append(views[1])
        observed = tuple(item.observed_at for item in quotes)
        if observed and (max(observed) - min(observed)).total_seconds() > (
            self._policy.risk.maximum_quote_skew_seconds
        ):
            raise PointInTimeInputUnavailable("逻辑账户多产品可成交报价时间偏差过大")
        return tuple(quotes)

    def _funding(self, *, as_of: datetime) -> tuple[FundingSettlement, ...]:
        start = self._funding_start
        if start is None:
            self._funding_start = as_of
            return ()
        if as_of <= start:
            raise ValueError("逻辑账户 Funding 时点必须递增")
        settlements = tuple(
            settlement
            for spec in self._policy.execution_specs
            if spec.instrument.product != InstrumentProduct.SPOT
            for settlement in self._market.funding_settlements(
                instrument=spec.instrument,
                start=start,
                end=as_of,
                visible_at=as_of,
            )
        )
        return tuple(
            sorted(
                settlements,
                key=lambda item: (
                    item.funding_time,
                    item.rate_type.value,
                    item.settlement_id,
                ),
            )
        )

    def _authorization(self, forecast: BaseForecast) -> CandidateCapitalAuthorization:
        return CandidateCapitalAuthorization(
            version=LOGICAL_ACCOUNT_EVALUATION_VERSION,
            producer_id=forecast.producer_id,
            producer_behavior_id=forecast.producer_behavior_id,
            outcome_family_id=forecast.outcome_family_id,
            hypothesis_fingerprint=content_hash(
                {
                    "evaluation_version": LOGICAL_ACCOUNT_EVALUATION_VERSION,
                    "capital_policy": content_hash(self._policy),
                    "producer_behavior_id": self._behavior_id,
                    "outcome_family_id": forecast.outcome_family_id,
                }
            ),
        )

    def _risk_profile(self, sleeve: PortfolioSleeveInput) -> SleeveRiskProfile:
        template = self._sleeve_risk
        return SleeveRiskProfile(
            sleeve_id=sleeve.sleeve_id,
            version=template.version,
            basis_stress_bps=template.basis_stress_bps,
            funding_stress_bps=template.funding_stress_bps,
            execution_stress_bps=template.execution_stress_bps,
            derivative_initial_margin_fraction=template.derivative_initial_margin_fraction,
        )

    def _projector(self, outcome_family_id: str) -> ProductPayoffBuilder:
        try:
            return self._product_payoffs[outcome_family_id]
        except KeyError as exc:
            raise ValueError(f"逻辑账户缺少 {outcome_family_id} 产品投影器") from exc

    @staticmethod
    def _holds_family(
        account: PortfolioAccountSnapshot | None,
        outcome_family_id: str,
    ) -> bool:
        return account is not None and any(
            item.forecast_family == outcome_family_id for item in account.sleeves
        )


def compare_producer_capital_paths(
    *,
    initial_cash: Decimal,
    sources: Mapping[str, tuple[ProducerPanelLedger, ProducerCapitalReplay]],
    allowed_strata: Collection[ForecastSlotStratum] | None = None,
    mark_at: datetime | None = None,
) -> ProducerCapitalComparisonEvidence | None:
    """Replay exact shared panels while preserving each producer's own latency and state."""

    if initial_cash <= 0 or len(sources) < 2:
        raise ValueError("Producer capital 对照需要至少两个来源和有效初始资金")
    included_strata = tuple(
        sorted(
            set(ForecastSlotStratum) if allowed_strata is None else set(allowed_strata),
            key=lambda item: item.value,
        )
    )
    if not included_strata:
        raise ValueError("Producer capital 对照至少需要一个样本分层")
    panels_by_source: dict[str, dict[tuple[str, ...], ProducerDecisionPanel]] = {}
    for label, (ledger, replay) in sources.items():
        if not label or ledger.producer_behavior_id != replay.producer_behavior_id:
            raise ValueError("Producer capital 来源标签或行为身份不一致")
        keyed: dict[tuple[str, ...], ProducerDecisionPanel] = {}
        for panel in ledger.complete_panels:
            panel_strata = {item.stratum for item in panel.slots}
            if len(panel_strata) != 1:
                raise ValueError("Producer capital panel 不能混合不同样本分层")
            if panel_strata.isdisjoint(included_strata):
                continue
            key = tuple(sorted(item.slot_id for item in panel.slots))
            if not key or key in keyed:
                raise ValueError("Producer capital panel 的 DecisionSlot 集必须唯一且非空")
            keyed[key] = panel
        panels_by_source[label] = keyed
    shared = set.intersection(*(set(items) for items in panels_by_source.values()))
    if not shared:
        return None
    shared_slot_sets = tuple(sorted(shared))
    latest_panel_at = max(
        panel.available_at
        for panels in panels_by_source.values()
        for key, panel in panels.items()
        if key in shared
    )
    if mark_at is not None:
        mark_at = require_utc(mark_at)
        if mark_at < latest_panel_at:
            raise ValueError("Producer capital 共同估值时点不能早于最后一个 panel")
    as_of = mark_at or latest_panel_at
    path_evidence: list[ProducerCapitalPathEvidence] = []
    for label, (ledger, replay) in sources.items():
        selected = tuple(
            panel
            for panel in ledger.complete_panels
            if tuple(sorted(item.slot_id for item in panel.slots)) in shared
        )
        steps = tuple(replay.advance(panel) for panel in selected)
        current = replay.account.current_account
        if current is not None and current.as_of < as_of:
            replay.mark(as_of=as_of)
        path = replay.account.result()
        path_evidence.append(
            ProducerCapitalPathEvidence(
                label=label,
                producer_id=selected[0].producer_id,
                producer_behavior_id=ledger.producer_behavior_id,
                panel_ids=tuple(item.panel_id for item in selected),
                steps=steps,
                path=path,
            )
        )
    values = {
        "evaluation_version": LOGICAL_ACCOUNT_EVALUATION_VERSION,
        "as_of": as_of,
        "initial_cash": initial_cash,
        "included_strata": included_strata,
        "shared_decision_slot_sets": shared_slot_sets,
        "paths": tuple(sorted(path_evidence, key=lambda item: item.label)),
    }
    return ProducerCapitalComparisonEvidence(
        comparison_id=stable_id("producer_capital_comparison", content_hash(values)),
        **values,
    )
