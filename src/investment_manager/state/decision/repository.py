from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from investment_manager.execution.models import AccountSnapshot
from investment_manager.information.models import IntelligenceEvent, SourceTier
from investment_manager.information.tables import source_observations
from investment_manager.market.models import (
    FeatureSnapshot,
    MarketSnapshot,
)
from investment_manager.state.decision.packet import (
    AnalysisMandate,
    DecisionPacket,
    DecisionPacketBuilder,
    VisibleFact,
)
from investment_manager.state.evidence_repository import SqlStateEvidenceStore, StateEvidenceKind
from investment_manager.state.facts import (
    validate_fact_revision_identity,
    validate_material_delta_identity,
    validate_state_snapshot_identity,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    MaterialDelta,
    StateSnapshot,
)
from investment_manager.state.policy import DecisionPacketPolicy
from investment_manager.state.repository import SqlFactStateStore
from investment_manager.state.tables import (
    canonical_fact_revisions,
    material_deltas,
)

_SOURCE_RANK = {
    SourceTier.FIRST_PARTY: 0,
    SourceTier.CONTRACTED: 1,
    SourceTier.AGGREGATOR: 2,
}


class SqlDecisionPacketAssembler:
    """Rebuild one Packet only from the exact evidence frozen by its State."""

    def __init__(self, engine: Engine, policy: DecisionPacketPolicy) -> None:
        self._engine = engine
        self._states = SqlFactStateStore(engine)
        self._evidence = SqlStateEvidenceStore(engine)
        self._builder = DecisionPacketBuilder(policy)

    def assemble(
        self,
        *,
        mandate: AnalysisMandate,
        state_id: str,
        delta_ids: tuple[str, ...],
        active_hypotheses: tuple[str, ...] = (),
        previous_assessment_refs: tuple[str, ...] = (),
    ) -> DecisionPacket:
        if len(set(delta_ids)) != len(delta_ids):
            raise ValueError("DecisionPacket delta_ids 不得重复")
        state = self._states.state(state_id)
        if state is None:
            raise ValueError("DecisionPacket 引用的 StateSnapshot 不存在")
        validate_state_snapshot_identity(state)
        if state.analysis_scope != mandate.analysis_scope:
            raise ValueError("DecisionPacket StateSnapshot 与 Mandate scope 不一致")
        deltas = self._load_deltas(delta_ids)
        facts = self._load_visible_facts(state.fact_revision_ids)
        markets, features, account, intelligence_events = self._load_state_evidence(state)
        return self._builder.build(
            mandate=mandate,
            state=state,
            deltas=deltas,
            facts=facts,
            intelligence_events=intelligence_events,
            account=account,
            markets=markets,
            features=features,
            active_hypotheses=active_hypotheses,
            previous_assessment_refs=previous_assessment_refs,
        )

    def _load_deltas(self, delta_ids: tuple[str, ...]) -> tuple[MaterialDelta, ...]:
        if not delta_ids:
            raise ValueError("DecisionPacket 必须引用至少一个 MaterialDelta")
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(material_deltas.c.delta_id, material_deltas.c.payload).where(
                    material_deltas.c.delta_id.in_(delta_ids)
                )
            ).all()
        by_id: dict[str, MaterialDelta] = {}
        for row in rows:
            delta = MaterialDelta.model_validate(row.payload)
            validate_material_delta_identity(delta)
            by_id[row.delta_id] = delta
        missing = tuple(sorted(set(delta_ids) - set(by_id)))
        if missing:
            raise ValueError("DecisionPacket 引用的 MaterialDelta 不存在: " + ", ".join(missing))
        return tuple(by_id[delta_id] for delta_id in delta_ids)

    def _load_visible_facts(
        self,
        revision_ids: tuple[str, ...],
    ) -> tuple[VisibleFact, ...]:
        if not revision_ids:
            return ()
        with self._engine.connect() as connection:
            fact_rows = connection.execute(
                select(
                    canonical_fact_revisions.c.revision_id,
                    canonical_fact_revisions.c.payload,
                ).where(canonical_fact_revisions.c.revision_id.in_(revision_ids))
            ).all()
            facts_by_id: dict[str, CanonicalFactRevision] = {}
            for row in fact_rows:
                fact = CanonicalFactRevision.model_validate(row.payload)
                validate_fact_revision_identity(fact)
                facts_by_id[row.revision_id] = fact
            missing_facts = tuple(sorted(set(revision_ids) - set(facts_by_id)))
            if missing_facts:
                raise ValueError(
                    "DecisionPacket 引用的 CanonicalFactRevision 不存在: "
                    + ", ".join(missing_facts)
                )
            observation_ids = tuple(
                sorted(
                    {
                        observation_id
                        for fact in facts_by_id.values()
                        for observation_id in fact.source_observation_ids
                    }
                )
            )
            observation_rows = connection.execute(
                select(
                    source_observations.c.observation_id,
                    source_observations.c.source_id,
                    source_observations.c.source_tier,
                ).where(source_observations.c.observation_id.in_(observation_ids))
            ).all()
        observations = {
            row.observation_id: (row.source_id, SourceTier(row.source_tier))
            for row in observation_rows
        }
        missing_observations = tuple(sorted(set(observation_ids) - set(observations)))
        if missing_observations:
            raise ValueError(
                "DecisionPacket Fact 缺少 SourceObservation: "
                + ", ".join(missing_observations)
            )

        visible: list[VisibleFact] = []
        for revision_id in revision_ids:
            fact = facts_by_id[revision_id]
            sources = tuple(
                observations[observation_id]
                for observation_id in fact.source_observation_ids
            )
            tiers = tuple(item[1] for item in sources)
            visible.append(
                VisibleFact(
                    fact=fact,
                    highest_source_tier=min(tiers, key=_SOURCE_RANK.__getitem__),
                    independent_source_count=len({item[0] for item in sources}),
                    prompt_injection_suspected=any(
                        tier != SourceTier.FIRST_PARTY for tier in tiers
                    ),
                )
            )
        return tuple(visible)

    def _load_state_evidence(
        self,
        state: StateSnapshot,
    ) -> tuple[
        tuple[MarketSnapshot, ...],
        tuple[FeatureSnapshot, ...],
        AccountSnapshot,
        tuple[IntelligenceEvent, ...],
    ]:
        if state.account_snapshot_ref is None:
            raise ValueError("DecisionPacket StateSnapshot 缺少 Account evidence ref")
        refs = (
            *state.market_snapshot_refs,
            *state.feature_snapshot_refs,
            *state.intelligence_event_refs,
            state.account_snapshot_ref,
        )
        evidence = self._evidence.get_many(refs)
        missing = tuple(sorted(set(refs) - set(evidence)))
        if missing:
            raise ValueError("DecisionPacket 缺少 State evidence: " + ", ".join(missing))

        markets = tuple(
            sorted(
                (
                    value
                    for kind, value in evidence.values()
                    if kind == StateEvidenceKind.MARKET
                    and isinstance(value, MarketSnapshot)
                ),
                key=lambda item: item.symbol,
            )
        )
        features = tuple(
            sorted(
                (
                    value
                    for kind, value in evidence.values()
                    if kind == StateEvidenceKind.FEATURE
                    and isinstance(value, FeatureSnapshot)
                ),
                key=lambda item: item.symbol,
            )
        )
        intelligence_events = tuple(
            sorted(
                (
                    value
                    for kind, value in evidence.values()
                    if kind == StateEvidenceKind.INTELLIGENCE
                    and isinstance(value, IntelligenceEvent)
                ),
                key=lambda item: item.evidence_id,
            )
        )
        account_entry = evidence[state.account_snapshot_ref]
        if account_entry[0] != StateEvidenceKind.ACCOUNT or not isinstance(
            account_entry[1], AccountSnapshot
        ):
            raise ValueError("DecisionPacket Account evidence 类型不一致")
        return markets, features, account_entry[1], intelligence_events
