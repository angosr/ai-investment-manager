from __future__ import annotations

from sqlalchemy.engine import Engine

from investment_manager.execution.account_repository import SqlAccountSnapshotReader
from investment_manager.execution.reconciliation_repository import (
    SqlReconciliationReportStore,
)
from investment_manager.market.features import FeatureEngine
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.settings import AppConfig
from investment_manager.state.application import DecisionPacketPreparation
from investment_manager.state.decision_packet_repository import SqlDecisionPacketAssembler
from investment_manager.state.projection import SqlFactStateProjector
from investment_manager.state.repository import SqlFactStateStore


def assemble_decision_packet_preparation(
    config: AppConfig,
    engine: Engine,
) -> DecisionPacketPreparation:
    """Compose the sole production path from point-in-time evidence to Packet."""

    return DecisionPacketPreparation(
        market_store=SqlMarketDataStore(engine),
        account_reader=SqlAccountSnapshotReader(
            engine,
            maximum_reconciliation_age_seconds=(
                config.reconciliation.maximum_report_age_seconds
            ),
            reports=SqlReconciliationReportStore(engine),
        ),
        facts=SqlFactStateStore(engine),
        projector=SqlFactStateProjector(
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
        initial_quote_balance=config.shadow.initial_quote_balance,
        maximum_market_age_seconds=config.risk.maximum_market_age_seconds,
    )
