"""Shared assembly of Context forecast targets and deterministic product mappings."""

from __future__ import annotations

from dataclasses import dataclass

from investment_manager.forecast.context.estimate import (
    ContextForecastTargetStateBehavior,
)
from investment_manager.forecast.context.producer import (
    MarketContextTargetStateProvider,
    context_forecast_contract,
)
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.product.context import ContextProductPayoffProjector
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.market.models import InstrumentId, SpotVenue
from investment_manager.market.policy import FeaturePolicy, MarketDataPolicy
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import (
    CapitalPolicy,
    ContextForecastTargetPolicy,
)


@dataclass(frozen=True, slots=True)
class ContextCapitalTargetDefinition:
    policy: ContextForecastTargetPolicy
    contract: ForecastContract
    instrument: InstrumentId
    state_behavior: ContextForecastTargetStateBehavior
    target_states: MarketContextTargetStateProvider
    product_payoffs: ContextProductPayoffProjector | None


def assemble_context_capital_targets(
    *,
    capital: CapitalPolicy,
    feature: FeaturePolicy,
    market_policy: MarketDataPolicy,
    market: MarketDataStore,
    product_store: SqlProductPayoffProjectionStore,
) -> tuple[ContextCapitalTargetDefinition, ...]:
    """Build the one target definition shared by production and evaluation."""

    context = capital.context_forecast
    if context is None or not context.enabled:
        return ()
    spec_by_key = {item.instrument.key: item for item in capital.execution_specs}
    reference_by_key = {item.key: item for item in capital.forecast_reference_instruments}
    forecast_instruments = {
        **{key: spec.instrument for key, spec in spec_by_key.items()},
        **reference_by_key,
    }
    perpetual_by_key = {item.key: item for item in market_policy.perpetual_instruments}
    cross_venue_symbols = (
        {item.symbol for item in market_policy.cross_venue_spot.products}
        if market_policy.cross_venue_spot is not None
        else set()
    )
    definitions: list[ContextCapitalTargetDefinition] = []
    for target_policy in context.targets:
        instrument = forecast_instruments[target_policy.reference_instrument_key]
        perpetual = (
            perpetual_by_key.get(target_policy.derivative_evidence_instrument_key)
            if target_policy.derivative_evidence_instrument_key is not None
            else None
        )
        comparison_policy = target_policy.comparison
        comparison = (
            perpetual_by_key[comparison_policy.instrument_key]
            if comparison_policy is not None
            else None
        )
        cross_venue_enabled = instrument.symbol in cross_venue_symbols
        behavior = ContextForecastTargetStateBehavior(
            feature_policy=feature,
            reference_instrument=instrument,
            derivative_evidence_instrument=perpetual,
            comparison_instrument=comparison,
            comparison_price_multiplier=(
                comparison_policy.reference_price_multiplier
                if comparison_policy is not None
                else None
            ),
            maximum_comparison_age_seconds=context.maximum_quote_age_seconds,
            interval=market_policy.interval,
            bar_window=market_policy.bar_window,
            funding_lookback_hours=market_policy.funding_history_lookback_hours,
            maximum_quote_skew_seconds=(market_policy.maximum_cross_market_quote_skew_seconds),
            cross_venue_spot_version=(
                market_policy.cross_venue_spot.version if cross_venue_enabled else None
            ),
            cross_venue_spot_venues=(
                tuple(sorted(SpotVenue, key=lambda item: item.value)) if cross_venue_enabled else ()
            ),
            maximum_cross_venue_spot_age_seconds=(
                market_policy.cross_venue_spot.maximum_age_seconds
                if cross_venue_enabled and market_policy.cross_venue_spot is not None
                else 30
            ),
        )
        contract = context_forecast_contract(
            policy=context,
            target_policy=target_policy,
            instrument=instrument,
            cost_semantics_version=capital.decision.cost_model_version,
        )
        target_states = MarketContextTargetStateProvider(
            market=market,
            feature_policy=behavior.feature_policy,
            reference=behavior.reference_instrument,
            perpetual=behavior.derivative_evidence_instrument,
            comparison=behavior.comparison_instrument,
            comparison_price_multiplier=behavior.comparison_price_multiplier,
            maximum_comparison_age_seconds=behavior.maximum_comparison_age_seconds,
            interval=behavior.interval,
            bar_window=behavior.bar_window,
            funding_lookback_hours=behavior.funding_lookback_hours,
            maximum_quote_skew_seconds=behavior.maximum_quote_skew_seconds,
            cross_venue_spot_venues=behavior.cross_venue_spot_venues,
            maximum_cross_venue_spot_age_seconds=(behavior.maximum_cross_venue_spot_age_seconds),
        )
        payoff_policy = target_policy.product_payoffs
        product_payoffs = None
        if payoff_policy is not None:
            payoff_specs = tuple(spec_by_key[key] for key in payoff_policy.instrument_keys)
            product_payoffs = ContextProductPayoffProjector(
                policy=payoff_policy,
                contract=contract,
                market=market,
                target_states=target_states,
                instruments=tuple(item.instrument for item in payoff_specs),
                execution_specs=payoff_specs,
                risk=capital.sleeve_risk,
                maximum_quote_age_seconds=context.maximum_quote_age_seconds,
                store=product_store,
            )
        definitions.append(
            ContextCapitalTargetDefinition(
                policy=target_policy,
                contract=contract,
                instrument=instrument,
                state_behavior=behavior,
                target_states=target_states,
                product_payoffs=product_payoffs,
            )
        )
    return tuple(definitions)
