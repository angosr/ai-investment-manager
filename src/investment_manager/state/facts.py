from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import Field, model_validator

from investment_manager.information.aggregated_flows import (
    CONTINUOUS_CONTEXT_FACT_TYPES,
    AggregatedEtfFlowSnapshot,
)
from investment_manager.information.models import DomainCoverageSnapshot, IntelligenceEvent
from investment_manager.information.official.metrics import (
    OfficialMetricSnapshot,
)
from investment_manager.information.official.public_calendar import (
    FedChairPublicEventRecord,
)
from investment_manager.information.official.records import (
    CalendarEventStatus,
    FedMonetaryReleaseRecord,
    MarketCalendarEventRevision,
    OfficialRecordKind,
)
from investment_manager.information.official.regulation import (
    FederalRegisterRulemakingRecord,
)
from investment_manager.information.official.treasury_buybacks import (
    TreasuryBuybackOperationRecord,
    TreasuryBuybackResultRecord,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.state.models import (
    CanonicalFactRevision,
    DeltaCategory,
    FactDecisionMateriality,
    FactRevisionStatus,
    MaterialDelta,
    Materiality,
    StateSnapshot,
)

FOMC_MEETING_FACT_TYPE = "FOMC_MEETING_SCHEDULE"
FED_CHAIR_PUBLIC_EVENT_FACT_TYPE = "FED_CHAIR_PUBLIC_EVENT_SCHEDULE"
FED_MONETARY_RELEASE_FACT_TYPE = "FED_MONETARY_RELEASE"
TREASURY_BUYBACK_OPERATION_FACT_TYPE = "TREASURY_BUYBACK_OPERATION_SCHEDULE"
TREASURY_BUYBACK_RESULT_FACT_TYPE = "TREASURY_BUYBACK_OPERATION_RESULT"
FEDERAL_REGISTER_RULEMAKING_FACT_TYPE = "US_DIGITAL_ASSET_RULEMAKING"


class OfficialFactProjectionPolicy(FrozenModel):
    version: str = Field(min_length=1)
    affected_assets: tuple[str, ...] = ()
    release_risk_factors: tuple[str, ...] = Field(
        default=("US_MONETARY_POLICY",),
        min_length=1,
    )
    metric_minimum_history_observations: int = Field(default=30, ge=10, le=5_000)
    metric_candidate_absolute_percentile: Decimal = Field(
        default=Decimal("0.95"),
        ge=Decimal("0.50"),
        le=Decimal("1"),
    )

    @model_validator(mode="after")
    def references_must_be_unique_and_sorted(self):
        for name in ("affected_assets", "release_risk_factors"):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} 必须唯一且排序")
        return self


class FactDeltaRule(FrozenModel):
    fact_type: str = Field(min_length=1, max_length=80)
    materiality: Materiality
    reason_code: str = Field(min_length=1, max_length=128)


class StateDeltaPolicy(FrozenModel):
    version: str = Field(min_length=1)
    validity_seconds: int = Field(gt=0, le=604_800)
    horizons_minutes: tuple[int, ...] = Field(min_length=1)
    rules: tuple[FactDeltaRule, ...] = Field(min_length=1)
    intelligence_materiality: Materiality = Materiality.NORMAL
    intelligence_risk_factors: tuple[str, ...] = Field(min_length=1)
    intelligence_reason_code: str = Field(min_length=1, max_length=128)
    market_materiality: Materiality = Materiality.HIGH
    market_risk_factors: tuple[str, ...] = Field(
        default=("MARKET_VOLATILITY",),
        min_length=1,
    )
    market_reason_code: str = Field(
        default="MARKET_SHOCK_TRIGGERED",
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def policy_must_be_unambiguous(self):
        if (
            tuple(sorted(set(self.horizons_minutes))) != self.horizons_minutes
            or any(value <= 0 for value in self.horizons_minutes)
        ):
            raise ValueError("horizons_minutes 必须为正数、唯一且排序")
        fact_types = tuple(rule.fact_type for rule in self.rules)
        if tuple(sorted(set(fact_types))) != fact_types:
            raise ValueError("FactDeltaRule 必须按 fact_type 唯一且排序")
        for name in ("intelligence_risk_factors", "market_risk_factors"):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} 必须唯一且排序")
        return self


def project_fomc_calendar_fact(
    revision: MarketCalendarEventRevision,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    fact_id = stable_id("canonical_fact", FOMC_MEETING_FACT_TYPE, revision.event_id)
    status = (
        FactRevisionStatus.ACTIVE
        if revision.status == CalendarEventStatus.SCHEDULED
        else FactRevisionStatus.CANCELLED
    )
    projection_label = (
        "with projections"
        if revision.has_projection_materials
        else "without projections"
    )
    claim = (
        f"FOMC meeting window {revision.event_start_at.isoformat()} to "
        f"{revision.event_end_at.isoformat()}; statement at "
        f"{revision.scheduled_release_at.isoformat()}; {projection_label}; "
        f"status {revision.status.value}."
    )
    return _build_fact_revision(
        fact_id=fact_id,
        projection_version=policy.version,
        fact_type=FOMC_MEETING_FACT_TYPE,
        status=status,
        event_time=revision.scheduled_release_at,
        observed_at=revision.observed_at,
        headline="FOMC monetary-policy statement schedule",
        claim=claim,
        affected_assets=policy.affected_assets,
        risk_factors=revision.risk_factors,
        decision_materiality=FactDecisionMateriality.BACKGROUND,
        source_observation_ids=(revision.source_observation_id,),
        previous=previous,
    )


def project_fed_monetary_release_fact(
    record: FedMonetaryReleaseRecord,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    observation = record.observation
    return _build_fact_revision(
        fact_id=stable_id(
            "canonical_fact",
            FED_MONETARY_RELEASE_FACT_TYPE,
            observation.source_id,
            observation.source_record_id,
        ),
        projection_version=policy.version,
        fact_type=FED_MONETARY_RELEASE_FACT_TYPE,
        status=FactRevisionStatus.ACTIVE,
        event_time=observation.source_published_at,
        observed_at=observation.observed_at,
        headline=record.title,
        claim=record.policy_state or record.summary or record.title,
        affected_assets=policy.affected_assets,
        risk_factors=policy.release_risk_factors,
        decision_materiality=FactDecisionMateriality.CANDIDATE,
        source_observation_ids=(observation.observation_id,),
        previous=previous,
    )


def project_federal_register_rulemaking_fact(
    record: FederalRegisterRulemakingRecord,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    """Project an official digital-asset rulemaking without predicting its effect."""

    legal_status = {
        "Notice": "notice-only",
        "Proposed Rule": "proposal-not-final",
        "Rule": "final-rule",
    }[record.document_type]
    dates = [f"published={record.publication_date.isoformat()}"]
    if record.effective_at is not None:
        dates.append(f"effective={record.effective_at.date().isoformat()}")
    if record.comments_close_at is not None:
        dates.append(f"comments_close={record.comments_close_at.date().isoformat()}")
    claim = (
        f"Federal Register {record.document_type}; legal_status={legal_status}; "
        f"document={record.document_number}; agencies={','.join(record.agencies)}; "
        f"{'; '.join(dates)}; action={record.action[:120] or 'not stated'}; "
        f"abstract={_head_tail(record.abstract, maximum=320)}."
    )
    return _build_fact_revision(
        fact_id=stable_id(
            "canonical_fact",
            FEDERAL_REGISTER_RULEMAKING_FACT_TYPE,
            record.document_number,
        ),
        projection_version=policy.version,
        fact_type=FEDERAL_REGISTER_RULEMAKING_FACT_TYPE,
        status=FactRevisionStatus.ACTIVE,
        event_time=datetime.combine(record.publication_date, time.min, tzinfo=UTC),
        observed_at=record.observation.observed_at,
        headline=record.title,
        claim=claim,
        affected_assets=policy.affected_assets,
        risk_factors=("REGULATION_LEGISLATION",),
        decision_materiality=FactDecisionMateriality.CANDIDATE,
        source_observation_ids=(record.observation.observation_id,),
        previous=previous,
    )


def _head_tail(value: str, *, maximum: int) -> str:
    """Preserve both scope and concluding clause in a bounded official summary."""

    compact = " ".join(value.split())
    if len(compact) <= maximum:
        return compact
    tail = maximum // 3
    return compact[: maximum - tail - 3].rstrip() + "..." + compact[-tail:].lstrip()


def project_fed_chair_public_event_fact(
    record: FedChairPublicEventRecord,
    revision: MarketCalendarEventRevision,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    observation = record.observation
    if (
        revision.event_type != OfficialRecordKind.FED_CHAIR_PUBLIC_EVENT
        or revision.source_observation_id != observation.observation_id
        or revision.source_record_id != observation.source_record_id
    ):
        raise ValueError("Fed Chair 事实投影记录与日历修订不一致")
    status = (
        FactRevisionStatus.ACTIVE
        if record.status == CalendarEventStatus.SCHEDULED
        else FactRevisionStatus.CANCELLED
    )
    details = "; ".join(
        item
        for item in (
            record.description,
            record.location,
            f"status {record.status.value}",
        )
        if item
    )
    return _build_fact_revision(
        fact_id=stable_id(
            "canonical_fact",
            FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
            revision.event_id,
        ),
        projection_version=policy.version,
        fact_type=FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
        status=status,
        event_time=record.scheduled_at,
        observed_at=observation.observed_at,
        headline=record.title,
        claim=(
            f"Federal Reserve Board Chair public event scheduled at "
            f"{record.scheduled_at.isoformat()}; {details}."
        ),
        affected_assets=policy.affected_assets,
        risk_factors=revision.risk_factors,
        decision_materiality=FactDecisionMateriality.BACKGROUND,
        source_observation_ids=(observation.observation_id,),
        previous=previous,
    )


def project_treasury_buyback_operation_fact(
    record: TreasuryBuybackOperationRecord,
    revision: MarketCalendarEventRevision,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    observation = record.observation
    if (
        revision.event_type != OfficialRecordKind.TREASURY_BUYBACK_OPERATION
        or revision.source_record_id != observation.source_record_id
    ):
        raise ValueError("Treasury buyback 事实投影记录与日历修订不一致")
    status = (
        FactRevisionStatus.ACTIVE
        if record.status == CalendarEventStatus.SCHEDULED
        else FactRevisionStatus.CANCELLED
    )
    claim = (
        "U.S. Treasury tentative buyback operation; "
        f"window={record.operation_start_at.isoformat()}.."
        f"{record.operation_end_at.isoformat()}; "
        f"settlement={record.settlement_date.isoformat()}; "
        f"operation_type={record.operation_type}; security_type={record.security_type}; "
        f"maturity_bucket={record.maturity_bucket}; "
        f"maturity_range={record.maturity_start.isoformat()}.."
        f"{record.maturity_end.isoformat()}; "
        f"scheduled_purchase_range_usd_m={record.minimum_purchase_usd_m}.."
        f"{record.maximum_purchase_usd_m}; status={record.status.value}. "
        "The maximum is a schedule ceiling, not an accepted purchase amount, "
        "and this Treasury operation is not Federal Reserve QE."
    )
    return _build_fact_revision(
        fact_id=stable_id(
            "canonical_fact",
            TREASURY_BUYBACK_OPERATION_FACT_TYPE,
            revision.event_id,
        ),
        projection_version=policy.version,
        fact_type=TREASURY_BUYBACK_OPERATION_FACT_TYPE,
        status=status,
        event_time=record.operation_start_at,
        observed_at=observation.observed_at,
        headline=(
            "U.S. Treasury tentative buyback operation: "
            f"{record.maturity_bucket}, up to ${record.maximum_purchase_usd_m}m"
        ),
        claim=claim,
        affected_assets=policy.affected_assets,
        risk_factors=revision.risk_factors,
        decision_materiality=FactDecisionMateriality.BACKGROUND,
        source_observation_ids=(observation.observation_id,),
        previous=previous,
    )


def project_treasury_buyback_result_fact(
    record: TreasuryBuybackResultRecord,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    utilization_pct = (
        record.accepted_usd_m / record.maximum_purchase_usd_m * Decimal("100")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    acceptance_pct = (
        record.accepted_usd_m / record.offered_usd_m * Decimal("100")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return _build_fact_revision(
        fact_id=stable_id(
            "canonical_fact",
            TREASURY_BUYBACK_RESULT_FACT_TYPE,
            record.operation_start_at.isoformat(),
        ),
        projection_version=policy.version,
        fact_type=TREASURY_BUYBACK_RESULT_FACT_TYPE,
        status=FactRevisionStatus.ACTIVE,
        event_time=record.operation_end_at,
        observed_at=record.observation.observed_at,
        headline=(
            "U.S. Treasury buyback result: "
            f"accepted ${record.accepted_usd_m}m of ${record.offered_usd_m}m offered"
        ),
        claim=(
            "U.S. Treasury buyback operation result; "
            f"window={record.operation_start_at.isoformat()}.."
            f"{record.operation_end_at.isoformat()}; "
            f"settlement={record.settlement_date.isoformat()}; "
            f"operation_type={record.operation_type}; security_type={record.security_type}; "
            f"maturity_bucket={record.maturity_bucket}; "
            f"maximum_purchase_usd_m={record.maximum_purchase_usd_m}; "
            f"offered_usd_m={record.offered_usd_m}; "
            f"accepted_usd_m={record.accepted_usd_m}; "
            f"maximum_utilization_pct={utilization_pct}; "
            f"offer_acceptance_pct={acceptance_pct}; "
            f"accepted_issues={record.accepted_issue_count}/"
            f"{record.eligible_issue_count}. "
            "This is the actual accepted Treasury redemption amount, not the "
            "tentative ceiling and not Federal Reserve QE. Its asset-price effect "
            "still requires synchronized yield, USD, cross-asset and spot-flow response."
        ),
        affected_assets=policy.affected_assets,
        risk_factors=("US_FISCAL_LIQUIDITY",),
        decision_materiality=FactDecisionMateriality.BACKGROUND,
        source_observation_ids=(record.observation.observation_id,),
        previous=previous,
    )


def project_official_metric_fact(
    record: OfficialMetricSnapshot,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    metrics = "; ".join(
        f"{item.name.value}={item.value} {item.unit.value}" for item in record.metrics
    )
    context = record.change_context
    decision_materiality = (
        FactDecisionMateriality.CANDIDATE
        if context is not None
        and context.sample_size >= policy.metric_minimum_history_observations
        and context.absolute_change_percentile
        >= policy.metric_candidate_absolute_percentile
        else FactDecisionMateriality.BACKGROUND
    )
    context_text = (
        ""
        if context is None
        else (
            f" change_context={context.metric_name.value}:"
            f"absolute_percentile={context.absolute_change_percentile},"
            f"sample_size={context.sample_size},"
            f"lookback={context.lookback_start.isoformat()}.."
            f"{context.lookback_end.isoformat()};"
        )
    )
    return _build_fact_revision(
        fact_id=stable_id("canonical_fact", record.fact_type),
        projection_version=policy.version,
        fact_type=record.fact_type,
        status=FactRevisionStatus.ACTIVE,
        event_time=datetime.combine(record.effective_date, time.min, tzinfo=UTC),
        observed_at=record.observation.observed_at,
        headline=record.headline,
        claim=f"effective_date={record.effective_date.isoformat()};{context_text} {metrics}.",
        affected_assets=policy.affected_assets,
        risk_factors=record.risk_factors,
        decision_materiality=decision_materiality,
        source_observation_ids=(record.observation.observation_id,),
        previous=previous,
    )


def project_aggregated_etf_flow_fact(
    record: AggregatedEtfFlowSnapshot,
    *,
    policy: OfficialFactProjectionPolicy,
    previous: CanonicalFactRevision | None = None,
) -> CanonicalFactRevision:
    decision_materiality = (
        FactDecisionMateriality.CANDIDATE
        if record.sample_size >= policy.metric_minimum_history_observations
        and record.absolute_flow_percentile
        >= policy.metric_candidate_absolute_percentile
        else FactDecisionMateriality.BACKGROUND
    )
    direction = "net inflow" if record.net_inflow_usd_m >= 0 else "net outflow"
    return _build_fact_revision(
        fact_id=stable_id("canonical_fact", record.fact_type),
        projection_version=policy.version,
        fact_type=record.fact_type,
        status=FactRevisionStatus.ACTIVE,
        event_time=datetime.combine(record.effective_date, time.min, tzinfo=UTC),
        observed_at=record.observation.observed_at,
        headline=(
            f"U.S. spot {record.asset} ETF aggregate daily {direction}: "
            f"${abs(record.net_inflow_usd_m)}m"
        ),
        claim=(
            f"aggregator=ByKaranteli; effective_date={record.effective_date.isoformat()}; "
            f"finalized_daily_net_inflow_usd_m={record.net_inflow_usd_m}; "
            f"net_assets_usd_m={record.net_assets_usd_m}; "
            f"cumulative_inflow_usd_m={record.cumulative_inflow_usd_m}; "
            f"value_traded_usd_m={record.value_traded_usd_m}; "
            f"absolute_flow_percentile={record.absolute_flow_percentile}; "
            f"sample_size={record.sample_size}; "
            f"lookback={record.lookback_start.isoformat()}..{record.lookback_end.isoformat()}. "
            "This is a finalized aggregate series from a named public aggregator, "
            "not issuer first-party evidence and not an intraday estimate."
        ),
        affected_assets=(record.asset,),
        risk_factors=(f"{record.asset}_INSTITUTIONAL_FLOW",),
        decision_materiality=decision_materiality,
        source_observation_ids=(record.observation.observation_id,),
        previous=previous,
    )


def build_state_snapshot(
    *,
    projection_version: str,
    analysis_scope: str,
    as_of: datetime,
    built_at: datetime,
    facts: tuple[CanonicalFactRevision, ...],
    market_snapshot_refs: tuple[str, ...] = (),
    feature_snapshot_refs: tuple[str, ...] = (),
    derivative_snapshot_refs: tuple[str, ...] = (),
    intelligence_event_refs: tuple[str, ...] = (),
    account_snapshot_ref: str | None = None,
    data_quality_codes: tuple[str, ...] = (),
    coverage_gap_codes: tuple[str, ...] = (),
    information_coverage: tuple[DomainCoverageSnapshot, ...] = (),
) -> StateSnapshot:
    as_of = require_utc(as_of)
    built_at = require_utc(built_at)
    fact_ids = tuple(item.fact_id for item in facts)
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("StateSnapshot 每个 fact_id 只能引用一个修订")
    if any(item.observed_at > as_of for item in facts):
        raise ValueError("StateSnapshot 不能引用 as_of 之后观察到的事实")
    fact_revision_ids = tuple(sorted(item.revision_id for item in facts))
    intelligence_refs = _unique_sorted(
        intelligence_event_refs,
        name="intelligence_event_refs",
    )
    payload = {
        "projection_version": projection_version,
        "analysis_scope": analysis_scope,
        "as_of": as_of.isoformat(),
        "fact_revision_ids": fact_revision_ids,
        "market_snapshot_refs": _unique_sorted(
            market_snapshot_refs, name="market_snapshot_refs"
        ),
        "feature_snapshot_refs": _unique_sorted(
            feature_snapshot_refs, name="feature_snapshot_refs"
        ),
        "account_snapshot_ref": account_snapshot_ref,
        "data_quality_codes": _unique_sorted(
            data_quality_codes, name="data_quality_codes"
        ),
        "coverage_gap_codes": _unique_sorted(
            coverage_gap_codes, name="coverage_gap_codes"
        ),
    }
    derivative_refs = _unique_sorted(
        derivative_snapshot_refs,
        name="derivative_snapshot_refs",
    )
    if derivative_refs:
        payload["derivative_snapshot_refs"] = derivative_refs
    if information_coverage:
        payload["information_coverage"] = information_coverage
    if intelligence_refs:
        payload["intelligence_event_refs"] = intelligence_refs
    digest = content_hash(payload)
    return StateSnapshot(
        state_id=stable_id("state_snapshot", digest),
        built_at=built_at,
        content_hash=digest,
        **payload,
    )


def build_state_material_delta(
    *,
    previous: StateSnapshot | None,
    current: StateSnapshot,
    current_facts: tuple[CanonicalFactRevision, ...],
    current_events: tuple[IntelligenceEvent, ...] = (),
    material_intelligence_event_refs: tuple[str, ...] | None = None,
    intelligence_affected_assets: tuple[str, ...] = (),
    market_feature_refs: tuple[str, ...] = (),
    market_affected_assets: tuple[str, ...] = (),
    policy: StateDeltaPolicy,
) -> MaterialDelta | None:
    if previous is None:
        return None
    if previous.analysis_scope != current.analysis_scope:
        raise ValueError("MaterialDelta 只能比较同一 analysis_scope")
    if previous.projection_version != current.projection_version:
        raise ValueError("MaterialDelta 不能比较不同 State projection version")
    if previous.as_of >= current.as_of:
        raise ValueError("MaterialDelta current state 必须晚于 previous state")

    facts_by_revision = {item.revision_id: item for item in current_facts}
    if len(facts_by_revision) != len(current_facts):
        raise ValueError("current_facts 不能重复 revision_id")
    if frozenset(facts_by_revision) != frozenset(current.fact_revision_ids):
        raise ValueError("current_facts 必须与 current StateSnapshot 完全一致")
    events_by_ref = {content_hash(item): item for item in current_events}
    if len(events_by_ref) != len(current_events):
        raise ValueError("current_events 不能包含重复内容")
    if frozenset(events_by_ref) != frozenset(current.intelligence_event_refs):
        raise ValueError("current_events 必须与 current StateSnapshot 完全一致")
    observed_changed_ids = tuple(
        sorted(set(current.fact_revision_ids) - set(previous.fact_revision_ids))
    )
    changed_ids = tuple(
        revision_id
        for revision_id in observed_changed_ids
        if (
            facts_by_revision[revision_id].fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES
            or facts_by_revision[revision_id].decision_materiality
            == FactDecisionMateriality.CANDIDATE
        )
    )
    if material_intelligence_event_refs is None:
        changed_event_refs = tuple(
            sorted(
                set(current.intelligence_event_refs)
                - set(previous.intelligence_event_refs)
            )
        )
    else:
        changed_event_refs = _unique_sorted(
            material_intelligence_event_refs,
            name="material_intelligence_event_refs",
        )
        if not set(changed_event_refs).issubset(current.intelligence_event_refs):
            raise ValueError(
                "material_intelligence_event_refs 必须属于 current StateSnapshot"
            )
        changed_event_refs = tuple(
            item
            for item in changed_event_refs
            if item not in previous.intelligence_event_refs
        )
    market_refs = _unique_sorted(market_feature_refs, name="market_feature_refs")
    if not set(market_refs).issubset(current.feature_snapshot_refs):
        raise ValueError("market_feature_refs 必须属于 current StateSnapshot")
    changed_market_refs = tuple(
        item for item in market_refs if item not in previous.feature_snapshot_refs
    )
    if not changed_ids and not changed_event_refs and not changed_market_refs:
        return None

    changed_facts = tuple(facts_by_revision[item] for item in changed_ids)
    changed_events = tuple(events_by_ref[item] for item in changed_event_refs)
    rules_by_type = {rule.fact_type: rule for rule in policy.rules}
    missing_rules = tuple(
        sorted({item.fact_type for item in changed_facts if item.fact_type not in rules_by_type})
    )
    if missing_rules:
        raise ValueError(f"StateDeltaPolicy 缺少规则: {', '.join(missing_rules)}")
    rules = tuple(rules_by_type[item.fact_type] for item in changed_facts)
    evidence_times = (
        tuple(item.observed_at for item in changed_facts)
        + tuple(item.observed_at for item in changed_events)
        + ((current.as_of,) if changed_market_refs else ())
    )
    observed_at = max(evidence_times)
    if observed_at > current.as_of:
        raise ValueError("MaterialDelta 不能引用 current as_of 之后的证据")
    materiality = max(
        (
            *(rule.materiality for rule in rules),
            *(
                (policy.intelligence_materiality,)
                if changed_event_refs
                else ()
            ),
            *((policy.market_materiality,) if changed_market_refs else ()),
        ),
        key=_materiality_rank,
    )
    event_assets = (
        _unique_sorted(
            intelligence_affected_assets,
            name="intelligence_affected_assets",
        )
        if changed_event_refs
        else ()
    )
    if changed_event_refs and not event_assets:
        raise ValueError("IntelligenceEvent MaterialDelta 缺少受影响资产")
    market_assets = (
        _unique_sorted(market_affected_assets, name="market_affected_assets")
        if changed_market_refs
        else ()
    )
    if changed_market_refs and not market_assets:
        raise ValueError("Market MaterialDelta 缺少受影响资产")
    payload = {
        "policy_version": policy.version,
        "analysis_scope": current.analysis_scope,
        "previous_state_id": previous.state_id,
        "current_state_id": current.state_id,
        "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(seconds=policy.validity_seconds)).isoformat(),
        "category": (
            DeltaCategory.CANONICAL_FACT.value
            if changed_ids
            else (
                DeltaCategory.INTELLIGENCE_EVENT.value
                if changed_event_refs
                else DeltaCategory.MARKET.value
            )
        ),
        "materiality": materiality.value,
        "affected_assets": tuple(
            sorted(
                {
                    *(asset for item in changed_facts for asset in item.affected_assets),
                    *event_assets,
                    *market_assets,
                }
            )
        ),
        "risk_factors": tuple(
            sorted(
                {
                    *(factor for item in changed_facts for factor in item.risk_factors),
                    *(
                        policy.intelligence_risk_factors
                        if changed_event_refs
                        else ()
                    ),
                    *(policy.market_risk_factors if changed_market_refs else ()),
                }
            )
        ),
        "horizons_minutes": policy.horizons_minutes,
        "fact_revision_ids": changed_ids,
        "feature_snapshot_refs": changed_market_refs,
        "reason_codes": tuple(
            sorted(
                {
                    *(rule.reason_code for rule in rules),
                    *(
                        (policy.intelligence_reason_code,)
                        if changed_event_refs
                        else ()
                    ),
                    *((policy.market_reason_code,) if changed_market_refs else ()),
                }
            )
        ),
    }
    if changed_event_refs:
        payload["intelligence_event_refs"] = changed_event_refs
    digest = content_hash(payload)
    return MaterialDelta(
        delta_id=stable_id("material_delta", digest),
        content_hash=digest,
        **payload,
    )


def validate_fact_revision_identity(fact: CanonicalFactRevision) -> None:
    expected_hash = content_hash(_fact_semantic_payload(fact))
    if fact.revision_hash != expected_hash:
        raise ValueError("CanonicalFactRevision revision_hash 与语义内容不一致")
    expected_id = stable_id(
        "canonical_fact_revision",
        fact.fact_id,
        fact.previous_revision_id or "ROOT",
        fact.revision_hash,
        fact.source_observation_ids,
    )
    if fact.revision_id != expected_id:
        raise ValueError("CanonicalFactRevision revision_id 与修订内容不一致")


def validate_state_snapshot_identity(state: StateSnapshot) -> None:
    expected_hash = content_hash(_state_identity_payload(state))
    if state.content_hash != expected_hash:
        raise ValueError("StateSnapshot content_hash 与状态内容不一致")
    if state.state_id != stable_id("state_snapshot", state.content_hash):
        raise ValueError("StateSnapshot state_id 与内容不一致")


def validate_material_delta_identity(delta: MaterialDelta) -> None:
    expected_hash = content_hash(_delta_identity_payload(delta))
    if delta.content_hash != expected_hash:
        raise ValueError("MaterialDelta content_hash 与变化内容不一致")
    if delta.delta_id != stable_id("material_delta", delta.content_hash):
        raise ValueError("MaterialDelta delta_id 与内容不一致")


def _build_fact_revision(
    *,
    fact_id: str,
    projection_version: str,
    fact_type: str,
    status: FactRevisionStatus,
    event_time: datetime | None,
    observed_at: datetime,
    headline: str,
    claim: str,
    affected_assets: tuple[str, ...],
    risk_factors: tuple[str, ...],
    decision_materiality: FactDecisionMateriality = FactDecisionMateriality.UNKNOWN,
    source_observation_ids: tuple[str, ...],
    previous: CanonicalFactRevision | None,
) -> CanonicalFactRevision:
    observed_at = require_utc(observed_at)
    if previous is not None:
        if previous.fact_id != fact_id:
            raise ValueError("前序事实修订不属于同一 fact_id")
        if previous.observed_at >= observed_at:
            raise ValueError("事实修订 observed_at 必须严格递增")
    semantic_payload = _fact_semantic_payload(
        CanonicalFactRevision.model_construct(
            fact_id=fact_id,
            projection_version=projection_version,
            fact_type=fact_type,
            status=status,
            event_time=event_time,
            headline=headline,
            claim=claim,
            affected_assets=_unique_sorted(affected_assets, name="affected_assets"),
            risk_factors=_unique_sorted(risk_factors, name="risk_factors"),
            decision_materiality=decision_materiality,
        )
    )
    revision_hash = content_hash(semantic_payload)
    if previous is not None and previous.revision_hash == revision_hash:
        raise ValueError("相同事实语义不得创建新修订")
    ordered_sources = _unique_sorted(
        source_observation_ids,
        name="source_observation_ids",
    )
    previous_revision_id = previous.revision_id if previous is not None else None
    return CanonicalFactRevision(
        revision_id=stable_id(
            "canonical_fact_revision",
            fact_id,
            previous_revision_id or "ROOT",
            revision_hash,
            ordered_sources,
        ),
        previous_revision_id=previous_revision_id,
        observed_at=observed_at,
        source_observation_ids=ordered_sources,
        revision_hash=revision_hash,
        **semantic_payload,
    )


def _unique_sorted(values: tuple, *, name: str) -> tuple:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} 不能包含重复值")
    return tuple(sorted(values))


def _fact_semantic_payload(fact: CanonicalFactRevision) -> dict:
    payload = {
        "projection_version": fact.projection_version,
        "fact_id": fact.fact_id,
        "fact_type": fact.fact_type,
        "status": fact.status.value,
        "event_time": fact.event_time.isoformat() if fact.event_time is not None else None,
        "headline": fact.headline,
        "claim": fact.claim,
        "affected_assets": fact.affected_assets,
        "risk_factors": fact.risk_factors,
    }
    # Preserve validation of revisions written before decision materiality was
    # introduced.  New explicit classifications are still identity-bearing.
    if fact.decision_materiality != FactDecisionMateriality.UNKNOWN:
        payload["decision_materiality"] = fact.decision_materiality.value
    return payload


def _state_identity_payload(state: StateSnapshot) -> dict:
    payload = {
        "projection_version": state.projection_version,
        "analysis_scope": state.analysis_scope,
        "as_of": state.as_of.isoformat(),
        "fact_revision_ids": state.fact_revision_ids,
        "market_snapshot_refs": state.market_snapshot_refs,
        "feature_snapshot_refs": state.feature_snapshot_refs,
        "account_snapshot_ref": state.account_snapshot_ref,
        "data_quality_codes": state.data_quality_codes,
        "coverage_gap_codes": state.coverage_gap_codes,
    }
    if state.intelligence_event_refs:
        payload["intelligence_event_refs"] = state.intelligence_event_refs
    if state.derivative_snapshot_refs:
        payload["derivative_snapshot_refs"] = state.derivative_snapshot_refs
    if state.information_coverage:
        payload["information_coverage"] = state.information_coverage
    return payload


def _delta_identity_payload(delta: MaterialDelta) -> dict:
    payload = {
        "policy_version": delta.policy_version,
        "analysis_scope": delta.analysis_scope,
        "previous_state_id": delta.previous_state_id,
        "current_state_id": delta.current_state_id,
        "observed_at": delta.observed_at.isoformat(),
        "expires_at": delta.expires_at.isoformat(),
        "category": delta.category.value,
        "materiality": delta.materiality.value,
        "affected_assets": delta.affected_assets,
        "risk_factors": delta.risk_factors,
        "horizons_minutes": delta.horizons_minutes,
        "fact_revision_ids": delta.fact_revision_ids,
        "feature_snapshot_refs": delta.feature_snapshot_refs,
        "reason_codes": delta.reason_codes,
    }
    if delta.intelligence_event_refs:
        payload["intelligence_event_refs"] = delta.intelligence_event_refs
    return payload


def _materiality_rank(value: Materiality) -> int:
    return {
        Materiality.LOW: 0,
        Materiality.NORMAL: 1,
        Materiality.HIGH: 2,
        Materiality.CRITICAL: 3,
    }[value]
