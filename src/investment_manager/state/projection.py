from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.engine import Engine

from investment_manager.execution.models import AccountSnapshot
from investment_manager.information.models import DomainCoverageSnapshot, IntelligenceEvent
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import (
    FeatureSnapshot,
    MarketSnapshot,
)
from investment_manager.market.perpetual.models import DerivativeContextSnapshot
from investment_manager.state.economic_reference import EconomicReferenceSnapshot
from investment_manager.state.evidence_repository import SqlStateEvidenceStore
from investment_manager.state.facts import (
    StateDeltaPolicy,
    build_state_material_delta,
    build_state_snapshot,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    MaterialDelta,
    StateSnapshot,
)
from investment_manager.state.repository import SqlFactStateStore


@dataclass(frozen=True, slots=True)
class StateProjectionResult:
    state: StateSnapshot
    delta: MaterialDelta | None
    changed: bool


class SqlStateProjector:
    """Sole assembly boundary for point-in-time State and MaterialDelta writes."""

    def __init__(
        self,
        engine: Engine,
        *,
        projection_version: str,
        delta_policy: StateDeltaPolicy,
    ) -> None:
        if not projection_version:
            raise ValueError("projection_version 不能为空")
        self._states = SqlFactStateStore(engine)
        self._evidence = SqlStateEvidenceStore(engine)
        self._projection_version = projection_version
        self._delta_policy = delta_policy

    @property
    def projection_version(self) -> str:
        return self._projection_version

    def project(
        self,
        *,
        analysis_scope: str,
        as_of: datetime,
        built_at: datetime,
        facts: tuple[CanonicalFactRevision, ...],
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        derivatives: tuple[DerivativeContextSnapshot, ...] = (),
        economic_references: tuple[EconomicReferenceSnapshot, ...] = (),
        account: AccountSnapshot | None = None,
        intelligence_events: tuple[IntelligenceEvent, ...] = (),
        material_intelligence_event_refs: tuple[str, ...] | None = None,
        intelligence_affected_assets: tuple[str, ...] = (),
        market_shock_symbols: tuple[str, ...] = (),
        market_affected_assets: tuple[str, ...] = (),
        data_quality_codes: tuple[str, ...] = (),
        coverage_gap_codes: tuple[str, ...] = (),
        information_coverage: tuple[DomainCoverageSnapshot, ...] = (),
    ) -> StateProjectionResult:
        as_of = require_utc(as_of)
        built_at = require_utc(built_at)
        self._require_complete_evidence(
            as_of=as_of,
            markets=markets,
            features=features,
            derivatives=derivatives,
            economic_references=economic_references,
            account=account,
            intelligence_events=intelligence_events,
        )
        candidate = build_state_snapshot(
            projection_version=self._projection_version,
            analysis_scope=analysis_scope,
            as_of=as_of,
            built_at=built_at,
            facts=facts,
            market_snapshot_refs=tuple(content_hash(item) for item in markets),
            feature_snapshot_refs=tuple(content_hash(item) for item in features),
            derivative_snapshot_refs=tuple(
                content_hash(item) for item in derivatives
            ),
            economic_reference_snapshot_refs=tuple(
                content_hash(item) for item in economic_references
            ),
            intelligence_event_refs=tuple(
                content_hash(item) for item in intelligence_events
            ),
            account_snapshot_ref=(content_hash(account) if account is not None else None),
            data_quality_codes=data_quality_codes,
            coverage_gap_codes=coverage_gap_codes,
            information_coverage=information_coverage,
        )
        feature_ref_by_symbol = {
            item.symbol: content_hash(item)
            for item in features
        }
        if tuple(sorted(set(market_shock_symbols))) != market_shock_symbols:
            raise ValueError("market_shock_symbols 必须唯一且排序")
        missing_market_symbols = tuple(
            item for item in market_shock_symbols if item not in feature_ref_by_symbol
        )
        if missing_market_symbols:
            raise ValueError(
                "Market shock 缺少 Feature evidence: "
                + ", ".join(missing_market_symbols)
            )
        existing = self._states.latest_state(
            analysis_scope=analysis_scope,
            projection_version=self._projection_version,
            as_of=as_of,
        )
        already_recorded = bool(
            existing is not None
            and existing.as_of == as_of
            and existing.state_id == candidate.state_id
        )
        if existing is not None and existing.as_of == as_of and not already_recorded:
            raise ValueError("同一 scope/projection/as_of 已存在不同 StateSnapshot")
        previous = self._states.latest_state_before(
            analysis_scope=analysis_scope,
            projection_version=self._projection_version,
            as_of=as_of,
        )
        self._persist_evidence(
            markets=markets,
            features=features,
            derivatives=derivatives,
            economic_references=economic_references,
            account=account,
            intelligence_events=intelligence_events,
        )
        if previous is None:
            state = self._states.record_state(
                state=candidate,
                previous_state_id=None,
            )
            return StateProjectionResult(
                state=state,
                delta=None,
                changed=not already_recorded,
            )

        delta = build_state_material_delta(
            previous=previous,
            current=candidate,
            current_facts=facts,
            current_events=intelligence_events,
            material_intelligence_event_refs=material_intelligence_event_refs,
            intelligence_affected_assets=intelligence_affected_assets,
            market_feature_refs=tuple(
                sorted(feature_ref_by_symbol[item] for item in market_shock_symbols)
            ),
            market_affected_assets=market_affected_assets,
            policy=self._delta_policy,
        )
        if delta is None:
            state = self._states.record_state(
                state=candidate,
                previous_state_id=previous.state_id,
            )
            return StateProjectionResult(
                state=state,
                delta=None,
                changed=not already_recorded,
            )
        state, stored_delta = self._states.record_transition(
            state=candidate,
            delta=delta,
        )
        return StateProjectionResult(
            state=state,
            delta=stored_delta,
            changed=not already_recorded,
        )

    def _persist_evidence(
        self,
        *,
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        derivatives: tuple[DerivativeContextSnapshot, ...],
        economic_references: tuple[EconomicReferenceSnapshot, ...],
        account: AccountSnapshot,
        intelligence_events: tuple[IntelligenceEvent, ...],
    ) -> None:
        for market in markets:
            self._evidence.put_market(market)
        for feature in features:
            self._evidence.put_feature(feature)
        for derivative in derivatives:
            self._evidence.put_derivative(derivative)
        for reference in economic_references:
            self._evidence.put_economic_reference(reference)
        if account is not None:
            self._evidence.put_account(account)
        for event in intelligence_events:
            self._evidence.put_intelligence(event)

    @staticmethod
    def _require_complete_evidence(
        *,
        as_of: datetime,
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        derivatives: tuple[DerivativeContextSnapshot, ...],
        economic_references: tuple[EconomicReferenceSnapshot, ...],
        account: AccountSnapshot | None,
        intelligence_events: tuple[IntelligenceEvent, ...],
    ) -> None:
        market_symbols = tuple(item.symbol for item in markets)
        feature_symbols = tuple(item.symbol for item in features)
        if len(set(market_symbols)) != len(market_symbols):
            raise ValueError("State projection 不能包含重复 Market symbol")
        if len(set(feature_symbols)) != len(feature_symbols):
            raise ValueError("State projection 不能包含重复 Feature symbol")
        if set(market_symbols) != set(feature_symbols):
            raise ValueError("State projection 的 Market/Feature symbols 必须完全一致")
        derivative_assets = tuple(item.asset for item in derivatives)
        derivative_symbols = tuple(item.instrument.symbol for item in derivatives)
        if len(set(derivative_assets)) != len(derivative_assets):
            raise ValueError("State projection 不能包含重复 Derivative asset")
        if len(set(derivative_symbols)) != len(derivative_symbols):
            raise ValueError("State projection 不能包含重复 Derivative symbol")
        if derivatives and set(derivative_symbols) != set(market_symbols):
            raise ValueError("State projection 的 Derivative/Market symbols 必须完全一致")
        if any(
            item.as_of > as_of or item.observed_at > as_of for item in markets
        ):
            raise ValueError("State projection 不能使用 as_of 之后的 Market evidence")
        if any(item.as_of > as_of for item in features):
            raise ValueError("State projection 不能使用 as_of 之后的 Feature evidence")
        if any(
            item.as_of != as_of or item.observed_at > as_of
            for item in derivatives
        ):
            raise ValueError("State projection 不能使用其他时点的 Derivative evidence")
        reference_assets = tuple(item.target_asset for item in economic_references)
        if tuple(sorted(set(reference_assets))) != reference_assets:
            raise ValueError("State projection 的经济交叉参考必须按目标资产唯一排序")
        if any(item.as_of != as_of for item in economic_references):
            raise ValueError("State projection 不能使用其他时点的经济交叉参考")
        if account is not None and (
            account.as_of > as_of or account.observed_at > as_of
        ):
            raise ValueError("State projection 不能使用 as_of 之后的 Account evidence")
        event_ids = tuple(item.evidence_id for item in intelligence_events)
        if tuple(sorted(set(event_ids))) != event_ids:
            raise ValueError("State projection IntelligenceEvent 必须按 evidence_id 唯一且排序")
        if any(item.observed_at > as_of for item in intelligence_events):
            raise ValueError("State projection 不能使用 as_of 之后的 IntelligenceEvent")
