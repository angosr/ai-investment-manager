from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.engine import Engine

from quant_core.asset_management import (
    CanonicalFactRevision,
    MaterialDelta,
    StateSnapshot,
)
from quant_core.domain import AccountSnapshot, FeatureSnapshot, MarketSnapshot, _require_utc
from quant_core.fact_pipeline import (
    FactDeltaPolicy,
    build_fact_material_delta,
    build_state_snapshot,
)
from quant_core.fact_state_sql import SqlFactStateStore
from quant_core.ids import content_hash
from quant_core.state_evidence_sql import SqlStateEvidenceStore


@dataclass(frozen=True, slots=True)
class FactStateProjectionResult:
    state: StateSnapshot
    delta: MaterialDelta | None
    changed: bool


class SqlFactStateProjector:
    """Sole assembly boundary for fact-driven State and MaterialDelta writes."""

    def __init__(
        self,
        engine: Engine,
        *,
        projection_version: str,
        delta_policy: FactDeltaPolicy,
    ) -> None:
        if not projection_version:
            raise ValueError("projection_version 不能为空")
        self._states = SqlFactStateStore(engine)
        self._evidence = SqlStateEvidenceStore(engine)
        self._projection_version = projection_version
        self._delta_policy = delta_policy

    def project(
        self,
        *,
        analysis_scope: str,
        as_of: datetime,
        built_at: datetime,
        facts: tuple[CanonicalFactRevision, ...],
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        account: AccountSnapshot,
        data_quality_codes: tuple[str, ...] = (),
        coverage_gap_codes: tuple[str, ...] = (),
    ) -> FactStateProjectionResult:
        as_of = _require_utc(as_of)
        built_at = _require_utc(built_at)
        self._require_complete_evidence(
            as_of=as_of,
            markets=markets,
            features=features,
            account=account,
        )
        candidate = build_state_snapshot(
            projection_version=self._projection_version,
            analysis_scope=analysis_scope,
            as_of=as_of,
            built_at=built_at,
            facts=facts,
            market_snapshot_refs=tuple(content_hash(item) for item in markets),
            feature_snapshot_refs=tuple(content_hash(item) for item in features),
            account_snapshot_ref=content_hash(account),
            data_quality_codes=data_quality_codes,
            coverage_gap_codes=coverage_gap_codes,
        )
        previous = self._states.latest_state_before(
            analysis_scope=analysis_scope,
            projection_version=self._projection_version,
            as_of=as_of,
        )
        if previous is not None and (
            previous.fact_revision_ids == candidate.fact_revision_ids
        ):
            return FactStateProjectionResult(
                state=previous,
                delta=None,
                changed=False,
            )

        self._persist_evidence(markets=markets, features=features, account=account)
        if previous is None:
            state = self._states.put_state(candidate)
            return FactStateProjectionResult(state=state, delta=None, changed=True)

        delta = build_fact_material_delta(
            previous=previous,
            current=candidate,
            current_facts=facts,
            policy=self._delta_policy,
        )
        if delta is None:
            raise ValueError("事实驱动 State 发生变化但没有可解释的 Fact MaterialDelta")
        state, stored_delta = self._states.record_transition(
            state=candidate,
            delta=delta,
        )
        return FactStateProjectionResult(
            state=state,
            delta=stored_delta,
            changed=True,
        )

    def _persist_evidence(
        self,
        *,
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        account: AccountSnapshot,
    ) -> None:
        for market in markets:
            self._evidence.put_market(market)
        for feature in features:
            self._evidence.put_feature(feature)
        self._evidence.put_account(account)

    @staticmethod
    def _require_complete_evidence(
        *,
        as_of: datetime,
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        account: AccountSnapshot,
    ) -> None:
        market_symbols = tuple(item.symbol for item in markets)
        feature_symbols = tuple(item.symbol for item in features)
        if len(set(market_symbols)) != len(market_symbols):
            raise ValueError("State projection 不能包含重复 Market symbol")
        if len(set(feature_symbols)) != len(feature_symbols):
            raise ValueError("State projection 不能包含重复 Feature symbol")
        if set(market_symbols) != set(feature_symbols):
            raise ValueError("State projection 的 Market/Feature symbols 必须完全一致")
        if any(
            item.as_of > as_of or item.observed_at > as_of for item in markets
        ):
            raise ValueError("State projection 不能使用 as_of 之后的 Market evidence")
        if any(item.as_of > as_of for item in features):
            raise ValueError("State projection 不能使用 as_of 之后的 Feature evidence")
        if account.as_of > as_of or account.observed_at > as_of:
            raise ValueError("State projection 不能使用 as_of 之后的 Account evidence")
