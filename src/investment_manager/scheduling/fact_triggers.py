from __future__ import annotations

from datetime import datetime, timedelta

from investment_manager.kernel.identity import stable_id
from investment_manager.scheduling.models import (
    AddWakeup,
    AnalysisTriggerType,
    DeleteWakeup,
    ScheduledWakeup,
    UpdateWakeup,
    build_trigger_event,
    build_trigger_plan_patch,
)
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.state.decision.packet import AnalysisMandate
from investment_manager.state.facts import FOMC_MEETING_FACT_TYPE, StateDeltaPolicy
from investment_manager.state.models import (
    CanonicalFactRevision,
    FactRevisionStatus,
    Materiality,
)
from investment_manager.state.repository import SqlFactStateStore

_PRIORITY = {
    Materiality.LOW: 60,
    Materiality.NORMAL: 75,
    Materiality.HIGH: 90,
    Materiality.CRITICAL: 100,
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
        required_freshness_seconds: int,
    ) -> None:
        if (
            trigger_expiry_seconds < 1
            or required_freshness_seconds < 1
            or not pipeline_id
        ):
            raise ValueError("CanonicalFact trigger expiry/freshness/pipeline 配置非法")
        self._facts = facts
        self._triggers = triggers
        self._symbols_by_asset = {
            item.asset: item.market_symbol for item in mandate.assets
        }
        self._assets_by_symbol = {
            symbol: asset for asset, symbol in self._symbols_by_asset.items()
        }
        self._rules = {item.fact_type: item for item in delta_policy.rules}
        self._pipeline_id = pipeline_id
        self._trigger_expiry_seconds = trigger_expiry_seconds
        self._required_freshness_seconds = required_freshness_seconds
        self._published_revision_ids: set[str] = set()

    def publish_recent(self, as_of: datetime) -> None:
        observed_since = as_of - timedelta(seconds=self._trigger_expiry_seconds)
        recent = self._facts.fact_revisions_observed_since(
            observed_since=observed_since,
            as_of=as_of,
        )
        self._published_revision_ids.intersection_update(
            fact.revision_id for fact in recent
        )
        for fact in recent:
            if fact.revision_id in self._published_revision_ids:
                continue
            try:
                rule = self._rules[fact.fact_type]
            except KeyError as exc:
                raise ValueError(
                    f"CanonicalFact 缺少 MaterialDelta 规则: {fact.fact_type}"
                ) from exc
            unknown_assets = tuple(
                sorted(set(fact.affected_assets) - set(self._symbols_by_asset))
            )
            if unknown_assets:
                raise ValueError(
                    "CanonicalFact affected_assets 不属于 Mandate: "
                    + ", ".join(unknown_assets)
                )
            occurred_at = min(fact.event_time or fact.observed_at, fact.observed_at)
            for asset in fact.affected_assets:
                trigger = build_trigger_event(
                    trigger_type=AnalysisTriggerType.CANONICAL_FACT_REVISED,
                    symbol=self._symbols_by_asset[asset],
                    pipeline_id=self._pipeline_id,
                    occurred_at=occurred_at,
                    observed_at=fact.observed_at,
                    priority=_PRIORITY[rule.materiality],
                    dedup_key=fact.revision_id,
                    evidence_ids=(fact.revision_id,),
                    expires_at=fact.observed_at
                    + timedelta(seconds=self._trigger_expiry_seconds),
                )
                self._triggers.record_trigger(trigger)
            self._published_revision_ids.add(fact.revision_id)
        self._sync_calendar(as_of)

    def _sync_calendar(self, as_of: datetime) -> None:
        facts = self._facts.facts_as_of(as_of=as_of)
        for symbol in self._symbols_by_asset.values():
            try:
                plan = self._triggers.plan_for_scope(
                    symbol=symbol,
                    pipeline_id=self._pipeline_id,
                )
            except KeyError:
                continue
            desired = {
                wakeup.wakeup_id: wakeup
                for wakeup in (
                    self._calendar_wakeup(fact, symbol=symbol)
                    for fact in facts
                    if (
                        fact.fact_type == FOMC_MEETING_FACT_TYPE
                        and fact.status == FactRevisionStatus.ACTIVE
                        and fact.event_time is not None
                        and fact.event_time > as_of
                        and self._assets_by_symbol[symbol] in fact.affected_assets
                    )
                )
            }
            existing = {
                item.wakeup_id: item
                for item in plan.scheduled_wakeups
                if item.wakeup_id.startswith("canonical_fact_wakeup_")
            }
            operations = []
            for wakeup_id in sorted(set(existing) - set(desired)):
                wakeup = existing[wakeup_id]
                if wakeup.wake_at <= as_of < wakeup.expires_at:
                    # The coordinator still owns delivery during the valid window.
                    continue
                operations.append(DeleteWakeup(wakeup_id=wakeup_id))
            for wakeup_id, wakeup in sorted(desired.items()):
                if wakeup_id not in existing:
                    operations.append(AddWakeup(wakeup=wakeup))
                elif existing[wakeup_id] != wakeup:
                    operations.append(UpdateWakeup(wakeup=wakeup))
            if not operations:
                continue
            self._triggers.apply_patch(
                build_trigger_plan_patch(
                    plan=plan,
                    submitted_at=as_of,
                    evidence_ids=tuple(
                        sorted(
                            {
                                evidence
                                for wakeup in desired.values()
                                for evidence in wakeup.evidence_ids
                            }
                        )
                    ),
                    operations=tuple(operations),
                ),
                now=as_of,
                current_manifest_id=plan.manifest_id,
            )

    def _calendar_wakeup(
        self,
        fact: CanonicalFactRevision,
        *,
        symbol: str,
    ) -> ScheduledWakeup:
        assert fact.event_time is not None
        return ScheduledWakeup(
            wakeup_id=stable_id("canonical_fact_wakeup", fact.fact_id, symbol),
            wake_at=fact.event_time,
            expires_at=fact.event_time
            + timedelta(seconds=self._trigger_expiry_seconds),
            reason=f"Official {fact.fact_type} scheduled release",
            evidence_ids=(fact.revision_id,),
            hypothesis="Reassess the portfolio after the official release becomes observable.",
            required_freshness_seconds=self._required_freshness_seconds,
        )
