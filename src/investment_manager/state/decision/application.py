from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from investment_manager.execution.account_repository import AccountSnapshotReader
from investment_manager.information.models import IntelligenceEvent
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.features import FeatureEngine
from investment_manager.market.repository import MarketDataStore
from investment_manager.state.decision.packet import (
    AnalysisMandate,
    DecisionPacket,
)
from investment_manager.state.decision.repository import SqlDecisionPacketAssembler
from investment_manager.state.projection import SqlStateProjector
from investment_manager.state.repository import SqlFactStateStore


class PacketPreparationStatus(StrEnum):
    BASELINE_RECORDED = "BASELINE_RECORDED"
    NO_MATERIAL_DELTA = "NO_MATERIAL_DELTA"
    MATERIAL_DELTA_EXPIRED = "MATERIAL_DELTA_EXPIRED"
    READY = "READY"


class DecisionPacketPreparationResult(FrozenModel):
    status: PacketPreparationStatus
    reason_code: str = Field(min_length=1)
    state_id: str = Field(min_length=1)
    delta_id: str | None = Field(default=None, min_length=1)
    packet: DecisionPacket | None = None

    @model_validator(mode="after")
    def shape_must_match_status(self):
        if self.status == PacketPreparationStatus.READY:
            if self.packet is None or self.delta_id is None:
                raise ValueError("READY Packet preparation 必须包含 Delta 和 Packet")
            if self.packet.state_id != self.state_id:
                raise ValueError("Prepared Packet 与 State identity 不一致")
            if self.packet.trigger_ids != (self.delta_id,):
                raise ValueError("Prepared Packet 必须且只能引用本次 MaterialDelta")
        elif self.packet is not None:
            raise ValueError("非 READY Packet preparation 不得携带 Packet")
        return self


class IntelligenceEventReader(Protocol):
    def exact(
        self,
        *,
        evidence_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[IntelligenceEvent, ...]: ...


class DecisionPacketPreparation:
    """Build one portfolio-wide Packet only when accepted evidence materially changes."""

    def __init__(
        self,
        *,
        market_store: MarketDataStore,
        account_reader: AccountSnapshotReader,
        event_reader: IntelligenceEventReader,
        facts: SqlFactStateStore,
        projector: SqlStateProjector,
        assembler: SqlDecisionPacketAssembler,
        features: FeatureEngine,
        market_interval: str,
        market_bar_window: int,
        market_source: str,
        initial_quote_balance: Decimal,
        maximum_market_age_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not market_interval or not market_source:
            raise ValueError("DecisionPacket market interval/source 不能为空")
        if market_bar_window < 2 or maximum_market_age_seconds < 1:
            raise ValueError("DecisionPacket market window/age 配置非法")
        self._market_store = market_store
        self._account_reader = account_reader
        self._event_reader = event_reader
        self._facts = facts
        self._projector = projector
        self._assembler = assembler
        self._features = features
        self._market_interval = market_interval
        self._market_bar_window = market_bar_window
        self._market_source = market_source
        self._initial_quote_balance = initial_quote_balance
        self._maximum_market_age_seconds = maximum_market_age_seconds
        self._clock = clock

    def prepare(
        self,
        *,
        analysis_id: str,
        as_of: datetime,
        mandate: AnalysisMandate,
        intelligence_evidence_ids: tuple[str, ...] = (),
        market_shock_symbols: tuple[str, ...] = (),
        active_hypotheses: tuple[str, ...] = (),
        previous_assessment_refs: tuple[str, ...] = (),
    ) -> DecisionPacketPreparationResult:
        if not analysis_id:
            raise ValueError("DecisionPacket analysis_id 不能为空")
        as_of = require_utc(as_of)
        if tuple(sorted(set(intelligence_evidence_ids))) != intelligence_evidence_ids:
            raise ValueError("intelligence_evidence_ids 必须唯一且排序")
        if tuple(sorted(set(market_shock_symbols))) != market_shock_symbols:
            raise ValueError("market_shock_symbols 必须唯一且排序")
        intelligence_events = self._event_reader.exact(
            evidence_ids=intelligence_evidence_ids,
            as_of=as_of,
        )
        symbol_to_asset = {
            item.market_symbol: item.asset
            for item in mandate.assets
        }
        missing_market_symbols = tuple(
            item for item in market_shock_symbols if item not in symbol_to_asset
        )
        if missing_market_symbols:
            raise ValueError(
                "Market shock 未命中 Mandate assets: "
                + ", ".join(missing_market_symbols)
            )
        intelligence_affected_assets = tuple(
            sorted(
                {
                    symbol_to_asset[symbol]
                    for event in intelligence_events
                    for symbol in event.symbols
                    if symbol in symbol_to_asset
                }
            )
        )
        if intelligence_events and not intelligence_affected_assets:
            raise ValueError("IntelligenceEvent 未命中 Mandate assets")
        market_affected_assets = tuple(
            sorted(symbol_to_asset[item] for item in market_shock_symbols)
        )
        markets = tuple(
            self._market_store.snapshot(
                cycle_id=analysis_id,
                symbol=asset.market_symbol,
                interval=self._market_interval,
                as_of=as_of,
                bar_window=self._market_bar_window,
                source=self._market_source,
            )
            for asset in mandate.assets
        )
        stale_symbols = tuple(
            market.symbol
            for market in markets
            if (as_of - market.observed_at).total_seconds()
            > self._maximum_market_age_seconds
        )
        if stale_symbols:
            raise ValueError("DecisionPacket 行情已过期: " + ", ".join(stale_symbols))
        feature_snapshots = tuple(self._features.compute(market) for market in markets)
        account = self._account_reader.account_for_cycle(
            cycle_id=analysis_id,
            as_of=as_of,
            initial_quote_balance=self._initial_quote_balance,
        )
        data_quality_codes = () if account.reconciled else ("ACCOUNT_UNRECONCILED",)
        projection = self._projector.project(
            analysis_scope=mandate.analysis_scope,
            as_of=as_of,
            built_at=max(require_utc(self._clock()), as_of),
            facts=self._facts.facts_as_of(as_of=as_of),
            markets=markets,
            features=feature_snapshots,
            account=account,
            intelligence_events=intelligence_events,
            intelligence_affected_assets=intelligence_affected_assets,
            market_shock_symbols=market_shock_symbols,
            market_affected_assets=market_affected_assets,
            data_quality_codes=data_quality_codes,
        )
        if projection.delta is None:
            baseline = not self._has_predecessor(
                mandate=mandate,
                as_of=as_of,
            )
            return DecisionPacketPreparationResult(
                status=(
                    PacketPreparationStatus.BASELINE_RECORDED
                    if baseline
                    else PacketPreparationStatus.NO_MATERIAL_DELTA
                ),
                reason_code=(
                    "STATE_BASELINE_RECORDED"
                    if baseline
                    else "NO_MATERIAL_STATE_CHANGE"
                ),
                state_id=projection.state.state_id,
            )
        delta = projection.delta
        if not projection.state.as_of < delta.expires_at:
            return DecisionPacketPreparationResult(
                status=PacketPreparationStatus.MATERIAL_DELTA_EXPIRED,
                reason_code="MATERIAL_DELTA_EXPIRED",
                state_id=projection.state.state_id,
                delta_id=delta.delta_id,
            )
        packet = self._assembler.assemble(
            mandate=mandate,
            state_id=projection.state.state_id,
            delta_ids=(delta.delta_id,),
            active_hypotheses=active_hypotheses,
            previous_assessment_refs=previous_assessment_refs,
        )
        return DecisionPacketPreparationResult(
            status=PacketPreparationStatus.READY,
            reason_code="DECISION_PACKET_READY",
            state_id=projection.state.state_id,
            delta_id=delta.delta_id,
            packet=packet,
        )

    def _has_predecessor(
        self,
        *,
        mandate: AnalysisMandate,
        as_of: datetime,
    ) -> bool:
        return (
            self._facts.latest_state_before(
                analysis_scope=mandate.analysis_scope,
                projection_version=self._projector.projection_version,
                as_of=as_of,
            )
            is not None
        )
