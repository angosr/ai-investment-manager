from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from investment_manager.execution.account_repository import AccountSnapshotReader
from investment_manager.information.coverage import SqlInformationCoverageStore
from investment_manager.information.models import DomainCoverageSnapshot, IntelligenceEvent
from investment_manager.information.policy import CoverageRequirement
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.features import (
    FeatureEngine,
    build_derivative_context_snapshot,
)
from investment_manager.market.models import InstrumentId, MarketSnapshot
from investment_manager.market.perpetual.models import DerivativeContextSnapshot
from investment_manager.market.repository import MarketDataStore
from investment_manager.state.decision.packet import (
    AnalysisMandate,
    DecisionPacket,
    PacketPreviousContext,
    PacketReviewRequest,
)
from investment_manager.state.decision.repository import SqlDecisionPacketAssembler
from investment_manager.state.projection import SqlStateProjector
from investment_manager.state.repository import SqlFactStateStore


class PacketPreparationStatus(StrEnum):
    BASELINE_RECORDED = "BASELINE_RECORDED"
    NO_MATERIAL_DELTA = "NO_MATERIAL_DELTA"
    MATERIAL_DELTA_EXPIRED = "MATERIAL_DELTA_EXPIRED"
    READY = "READY"


class DecisionPacketPreparationError(ValueError):
    """State transition exists, but its immutable Packet could not be assembled."""


class DecisionPacketPreparationResult(FrozenModel):
    status: PacketPreparationStatus
    reason_code: str = Field(min_length=1)
    state_id: str = Field(min_length=1)
    delta_id: str | None = Field(default=None, min_length=1)
    review_ids: tuple[str, ...] = ()
    packet: DecisionPacket | None = None

    @model_validator(mode="after")
    def shape_must_match_status(self):
        if self.status == PacketPreparationStatus.READY:
            if self.packet is None:
                raise ValueError("READY Packet preparation 必须包含 Packet")
            if self.packet.state_id != self.state_id:
                raise ValueError("Prepared Packet 与 State identity 不一致")
            expected = (
                *((self.delta_id,) if self.delta_id is not None else ()),
                *self.review_ids,
            )
            if not expected or self.packet.trigger_ids != expected:
                raise ValueError("Prepared Packet 必须且只能引用本次分析原因")
        elif self.packet is not None:
            raise ValueError("非 READY Packet preparation 不得携带 Packet")
        elif self.review_ids:
            raise ValueError("非 READY Packet preparation 不得携带 review_ids")
        return self


class IntelligenceEventReader(Protocol):
    def exact(
        self,
        *,
        evidence_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[IntelligenceEvent, ...]: ...

    def visible(
        self,
        *,
        symbol: str,
        as_of: datetime,
    ) -> tuple[IntelligenceEvent, ...]: ...


class DecisionPacketPreparation:
    """Build one portfolio Packet for a material change or explicit Agent review."""

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
        coverage_reader: SqlInformationCoverageStore | None = None,
        coverage_requirements: tuple[CoverageRequirement, ...] = (),
        perpetual_instruments: tuple[InstrumentId, ...] = (),
        funding_history_lookback_hours: int = 24,
        maximum_perpetual_age_seconds: int = 900,
        maximum_cross_market_quote_skew_seconds: int = 15,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not market_interval or not market_source:
            raise ValueError("DecisionPacket market interval/source 不能为空")
        if market_bar_window < 2 or maximum_market_age_seconds < 1:
            raise ValueError("DecisionPacket market window/age 配置非法")
        if not 8 <= funding_history_lookback_hours <= 720:
            raise ValueError("DecisionPacket Funding 窗口配置非法")
        if maximum_perpetual_age_seconds < 1:
            raise ValueError("DecisionPacket Perpetual age 配置非法")
        if maximum_cross_market_quote_skew_seconds < 1:
            raise ValueError("DecisionPacket 跨市场报价偏差配置非法")
        perpetual_symbols = tuple(item.symbol for item in perpetual_instruments)
        if tuple(sorted(set(perpetual_symbols))) != perpetual_symbols:
            raise ValueError("DecisionPacket perpetual_instruments 必须按 symbol 唯一且排序")
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
        self._coverage_reader = coverage_reader
        self._coverage_requirements = coverage_requirements
        self._perpetual_by_symbol = {
            item.symbol: item for item in perpetual_instruments
        }
        self._funding_history_lookback_hours = funding_history_lookback_hours
        self._maximum_perpetual_age_seconds = maximum_perpetual_age_seconds
        self._maximum_cross_market_quote_skew_seconds = (
            maximum_cross_market_quote_skew_seconds
        )
        self._clock = clock

    def prepare(
        self,
        *,
        analysis_id: str,
        as_of: datetime,
        mandate: AnalysisMandate,
        intelligence_evidence_ids: tuple[str, ...] = (),
        market_shock_symbols: tuple[str, ...] = (),
        review_requests: tuple[PacketReviewRequest, ...] = (),
        previous_context: PacketPreviousContext | None = None,
    ) -> DecisionPacketPreparationResult:
        if not analysis_id:
            raise ValueError("DecisionPacket analysis_id 不能为空")
        as_of = require_utc(as_of)
        if tuple(sorted(set(intelligence_evidence_ids))) != intelligence_evidence_ids:
            raise ValueError("intelligence_evidence_ids 必须唯一且排序")
        if tuple(sorted(set(market_shock_symbols))) != market_shock_symbols:
            raise ValueError("market_shock_symbols 必须唯一且排序")
        if tuple(sorted(set(item.review_id for item in review_requests))) != tuple(
            item.review_id for item in review_requests
        ):
            raise ValueError("review_requests 必须按 review_id 唯一且排序")
        if any(item.requested_at > as_of for item in review_requests):
            raise ValueError("review_requests 不能晚于 DecisionPacket as_of")
        triggered_events = self._event_reader.exact(
            evidence_ids=intelligence_evidence_ids,
            as_of=as_of,
        )
        # A trigger explains why analysis runs; it is not the whole world state.
        # Rebuild a bounded recent context from the reader for every mandate asset,
        # then let DecisionPacketPolicy rank and cap what reaches the model.  The
        # exact triggered items are merged back even when a reader's visible limit
        # would otherwise omit them.
        visible_by_id: dict[str, IntelligenceEvent] = {
            event.evidence_id: event
            for asset in mandate.assets
            for event in self._event_reader.visible(
                symbol=asset.market_symbol,
                as_of=as_of,
            )
        }
        for event in triggered_events:
            existing = visible_by_id.get(event.evidence_id)
            if existing is not None and existing != event:
                raise ValueError("相同 evidence_id 的可见事件内容不一致")
            visible_by_id[event.evidence_id] = event
        intelligence_events = tuple(
            visible_by_id[evidence_id]
            for evidence_id in sorted(visible_by_id)
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
                    for event in triggered_events
                    for symbol in event.symbols
                    if symbol in symbol_to_asset
                }
            )
        )
        if triggered_events and not intelligence_affected_assets:
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
        derivatives = self._derivative_context(
            analysis_id=analysis_id,
            as_of=as_of,
            mandate=mandate,
            markets=markets,
        )
        account = self._account_reader.account_for_cycle(
            cycle_id=analysis_id,
            as_of=as_of,
            initial_quote_balance=self._initial_quote_balance,
        )
        data_quality_codes = () if account.reconciled else ("ACCOUNT_UNRECONCILED",)
        information_coverage: tuple[DomainCoverageSnapshot, ...] = ()
        coverage_gap_codes: tuple[str, ...] = ()
        if self._coverage_reader is not None:
            information_coverage = self._coverage_reader.snapshot(
                as_of=as_of,
                requirements=self._coverage_requirements,
            )
            coverage_gap_codes = self._coverage_reader.gap_codes(
                information_coverage
            )
        projection = self._projector.project(
            analysis_scope=mandate.analysis_scope,
            as_of=as_of,
            built_at=max(require_utc(self._clock()), as_of),
            facts=self._facts.facts_as_of(as_of=as_of),
            markets=markets,
            features=feature_snapshots,
            derivatives=derivatives,
            account=account,
            intelligence_events=intelligence_events,
            material_intelligence_event_refs=tuple(
                sorted(content_hash(item) for item in triggered_events)
            ),
            intelligence_affected_assets=intelligence_affected_assets,
            market_shock_symbols=market_shock_symbols,
            market_affected_assets=market_affected_assets,
            data_quality_codes=data_quality_codes,
            coverage_gap_codes=coverage_gap_codes,
            information_coverage=information_coverage,
        )

        if projection.delta is None and not review_requests:
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
        if delta is not None and not projection.state.as_of < delta.expires_at:
            return DecisionPacketPreparationResult(
                status=PacketPreparationStatus.MATERIAL_DELTA_EXPIRED,
                reason_code="MATERIAL_DELTA_EXPIRED",
                state_id=projection.state.state_id,
                delta_id=delta.delta_id,
            )
        try:
            packet = self._assembler.assemble(
                mandate=mandate,
                state_id=projection.state.state_id,
                delta_ids=((delta.delta_id,) if delta is not None else ()),
                review_requests=review_requests,
                previous_context=previous_context,
            )
        except ValueError as exc:
            # Projection persists State/Delta before Packet assembly. Callers must
            # retry this exact point-in-time input instead of advancing as_of.
            raise DecisionPacketPreparationError(
                "分析原因已冻结但 DecisionPacket 组装失败"
            ) from exc
        return DecisionPacketPreparationResult(
            status=PacketPreparationStatus.READY,
            reason_code=(
                "EXPLICIT_REVIEW_PACKET_READY"
                if delta is None
                else "DECISION_PACKET_READY"
            ),
            state_id=projection.state.state_id,
            delta_id=delta.delta_id if delta is not None else None,
            review_ids=tuple(item.review_id for item in review_requests),
            packet=packet,
        )

    def _derivative_context(
        self,
        *,
        analysis_id: str,
        as_of: datetime,
        mandate: AnalysisMandate,
        markets: tuple[MarketSnapshot, ...],
    ) -> tuple[DerivativeContextSnapshot, ...]:
        if not self._perpetual_by_symbol:
            return ()
        mandate_symbols = tuple(item.market_symbol for item in mandate.assets)
        if set(self._perpetual_by_symbol) != set(mandate_symbols):
            raise ValueError("DecisionPacket Perpetual universe 与 Mandate 不一致")
        market_by_symbol = {item.symbol: item for item in markets}
        snapshots: list[DerivativeContextSnapshot] = []
        for asset in mandate.assets:
            instrument = self._perpetual_by_symbol[asset.market_symbol]
            state = self._market_store.latest_perpetual_state(
                instrument=instrument,
                as_of=as_of,
            )
            quote = self._market_store.latest_perpetual_quote(
                instrument=instrument,
                evaluation_at=as_of,
                visible_at=as_of,
            )
            if state is None or quote is None:
                raise ValueError(
                    f"DecisionPacket 缺少 {asset.market_symbol} Perpetual 状态或报价"
                )
            if max(
                (as_of - state.observed_at).total_seconds(),
                (as_of - quote.observed_at).total_seconds(),
            ) > self._maximum_perpetual_age_seconds:
                raise ValueError(
                    f"DecisionPacket {asset.market_symbol} Perpetual 行情已过期"
                )
            aligned_spot_quote = self._market_store.latest_spot_quote(
                instrument=InstrumentId.binance_spot(
                    symbol=asset.market_symbol,
                    base_asset=asset.asset,
                    quote_asset=instrument.quote_asset,
                ),
                evaluation_at=quote.observed_at,
                visible_at=as_of,
            )
            if aligned_spot_quote is None:
                raise ValueError(
                    f"DecisionPacket 缺少 {asset.market_symbol} 点时对齐 Spot 报价"
                )
            settlements = self._market_store.funding_settlements(
                instrument=instrument,
                start=as_of - timedelta(hours=self._funding_history_lookback_hours),
                end=as_of,
                visible_at=as_of,
            )
            snapshots.append(
                build_derivative_context_snapshot(
                    cycle_id=analysis_id,
                    asset=asset.asset,
                    spot=market_by_symbol[asset.market_symbol],
                    aligned_spot_quote=aligned_spot_quote,
                    state=state,
                    quote=quote,
                    settlements=settlements,
                    funding_window_hours=self._funding_history_lookback_hours,
                    maximum_quote_skew_seconds=(
                        self._maximum_cross_market_quote_skew_seconds
                    ),
                )
            )
        return tuple(sorted(snapshots, key=lambda item: item.asset))

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
