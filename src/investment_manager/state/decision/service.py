from __future__ import annotations

from sqlalchemy.engine import Engine

from investment_manager.information.coverage import SqlInformationCoverageStore
from investment_manager.information.repository import SqlEventStore
from investment_manager.market.features import FeatureEngine
from investment_manager.market.models import SpotVenue
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.settings import AppConfig
from investment_manager.state.decision.application import DecisionPacketPreparation
from investment_manager.state.decision.repository import SqlDecisionPacketAssembler
from investment_manager.state.projection import SqlStateProjector
from investment_manager.state.repository import SqlFactStateStore


def assemble_decision_packet_preparation(
    config: AppConfig,
    engine: Engine,
) -> DecisionPacketPreparation:
    """Compose the sole production path from point-in-time evidence to Packet."""

    return DecisionPacketPreparation(
        market_store=SqlMarketDataStore(engine),
        event_reader=SqlEventStore(engine),
        facts=SqlFactStateStore(engine),
        projector=SqlStateProjector(
            engine,
            projection_version=config.decision_state.version,
            delta_policy=config.decision_state.delta_policy,
        ),
        assembler=SqlDecisionPacketAssembler(
            engine,
            config.decision_state.packet_policy,
        ),
        features=FeatureEngine(config.feature),
        market_interval=config.market_data.interval,
        market_bar_window=config.market_data.bar_window,
        market_source=config.market_data.version,
        maximum_market_age_seconds=(
            config.decision_state.packet_policy.maximum_market_age_seconds
        ),
        coverage_reader=SqlInformationCoverageStore(engine),
        coverage_requirements=config.information.coverage_requirements,
        perpetual_instruments=config.market_data.perpetual_instruments,
        funding_history_lookback_hours=(
            config.market_data.funding_history_lookback_hours
        ),
        maximum_perpetual_age_seconds=config.market_data.perpetual_poll_seconds * 3,
        maximum_cross_market_quote_skew_seconds=(
            config.market_data.maximum_cross_market_quote_skew_seconds
        ),
        cross_venue_spot_venues=(
            tuple(sorted(SpotVenue, key=lambda item: item.value))
            if config.market_data.cross_venue_spot is not None
            else ()
        ),
        maximum_cross_venue_spot_age_seconds=(
            config.market_data.cross_venue_spot.maximum_age_seconds
            if config.market_data.cross_venue_spot is not None
            else 30
        ),
    )
