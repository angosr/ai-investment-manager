from __future__ import annotations

from datetime import datetime, timedelta

from investment_manager.information.aggregated_flows import CONTINUOUS_CONTEXT_FACT_TYPES
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    build_trigger_event,
)
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.state.decision.packet import AnalysisMandate
from investment_manager.state.facts import (
    ECONOMIC_RELEASE_EVENT_FACT_TYPE,
    FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
    StateDeltaPolicy,
)
from investment_manager.state.models import (
    FactDecisionMateriality,
    Materiality,
)
from investment_manager.state.repository import SqlFactStateStore

_PRIORITY = {
    Materiality.LOW: 60,
    Materiality.NORMAL: 75,
    Materiality.HIGH: 90,
    Materiality.CRITICAL: 100,
}
_SCHEDULED_FACT_TYPES = {
    ECONOMIC_RELEASE_EVENT_FACT_TYPE,
    FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
}
class CanonicalFactTriggerPublisher:
    """Idempotently publish recent material fact revisions into Scheduling."""

    def __init__(
        self,
        *,
        facts: SqlFactStateStore,
        triggers: SqlTriggerRepository,
        mandate: AnalysisMandate,
        delta_policy: StateDeltaPolicy,
        pipeline_id: str,
        trigger_expiry_seconds: int,
        analysis_owner_symbol: str | None = None,
    ) -> None:
        if trigger_expiry_seconds < 1 or not pipeline_id:
            raise ValueError("CanonicalFact trigger expiry/freshness/pipeline 配置非法")
        self._facts = facts
        self._triggers = triggers
        self._symbols_by_asset = {
            item.asset: item.market_symbol for item in mandate.observation_assets
        }
        self._assets_by_symbol = {symbol: asset for asset, symbol in self._symbols_by_asset.items()}
        if (
            analysis_owner_symbol is not None
            and analysis_owner_symbol not in self._assets_by_symbol
        ):
            raise ValueError("CanonicalFact analysis owner 不属于 Mandate")
        self._analysis_owner_symbol = analysis_owner_symbol
        self._rules = {item.fact_type: item for item in delta_policy.rules}
        self._pipeline_id = pipeline_id
        self._trigger_expiry_seconds = trigger_expiry_seconds
        self._published_revision_ids: set[str] = set()

    def publish_recent(self, as_of: datetime) -> None:
        observed_since = as_of - timedelta(seconds=self._trigger_expiry_seconds)
        recent = self._facts.fact_revisions_observed_since(
            observed_since=observed_since,
            as_of=as_of,
        )
        self._published_revision_ids.intersection_update(fact.revision_id for fact in recent)
        for fact in recent:
            if fact.revision_id in self._published_revision_ids:
                continue
            try:
                rule = self._rules[fact.fact_type]
            except KeyError as exc:
                raise ValueError(
                    f"CanonicalFact 缺少 MaterialDelta 规则: {fact.fact_type}"
                ) from exc
            if (
                fact.fact_type in CONTINUOUS_CONTEXT_FACT_TYPES
                and fact.decision_materiality != FactDecisionMateriality.CANDIDATE
            ):
                # Routine continuous observations remain in State but do not
                # spend an event-driven AI call.  A later material event or
                # explicit review still sees the latest background values.
                self._published_revision_ids.add(fact.revision_id)
                continue
            if fact.fact_type in _SCHEDULED_FACT_TYPES:
                # A calendar is durable future state.  Synchronize its wakeup
                # below; initial discovery and rescheduling do not justify a
                # separate AI call before the normal portfolio review.
                self._published_revision_ids.add(fact.revision_id)
                continue
            unknown_assets = tuple(sorted(set(fact.affected_assets) - set(self._symbols_by_asset)))
            if unknown_assets:
                raise ValueError(
                    "CanonicalFact affected_assets 不属于 Mandate: " + ", ".join(unknown_assets)
                )
            occurred_at = min(fact.event_time or fact.observed_at, fact.observed_at)
            directly_affected_symbols = tuple(
                sorted(self._symbols_by_asset[asset] for asset in fact.affected_assets)
            )
            routing_symbols = (
                {self._analysis_owner_symbol}
                if self._analysis_owner_symbol is not None
                else set(directly_affected_symbols)
            )
            for symbol in sorted(routing_symbols):
                trigger = build_trigger_event(
                    trigger_type=AnalysisTriggerType.CANONICAL_FACT_REVISED,
                    symbol=symbol,
                    pipeline_id=self._pipeline_id,
                    occurred_at=occurred_at,
                    observed_at=fact.observed_at,
                    priority=_PRIORITY[rule.materiality],
                    dedup_key=fact.revision_id,
                    evidence_ids=(fact.revision_id,),
                    affected_symbols=(
                        directly_affected_symbols if self._analysis_owner_symbol is not None else ()
                    ),
                    expires_at=fact.observed_at + timedelta(seconds=self._trigger_expiry_seconds),
                )
                self._triggers.record_trigger(trigger)
            self._published_revision_ids.add(fact.revision_id)
