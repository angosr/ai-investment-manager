from __future__ import annotations

from enum import StrEnum

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.models import AccountSnapshot
from investment_manager.information.models import IntelligenceEvent
from investment_manager.kernel.identity import content_hash
from investment_manager.market.models import (
    FeatureSnapshot,
    MarketSnapshot,
)
from investment_manager.market.perpetual.models import DerivativeContextSnapshot
from investment_manager.state.tables import state_evidence_snapshots


class StateEvidenceKind(StrEnum):
    MARKET = "MARKET"
    FEATURE = "FEATURE"
    DERIVATIVE = "DERIVATIVE"
    ACCOUNT = "ACCOUNT"
    INTELLIGENCE = "INTELLIGENCE"


StateEvidence = (
    MarketSnapshot
    | FeatureSnapshot
    | DerivativeContextSnapshot
    | AccountSnapshot
    | IntelligenceEvent
)


class SqlStateEvidenceStore:
    """One immutable content-addressed ledger for StateSnapshot input objects."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put_market(self, snapshot: MarketSnapshot) -> str:
        return self._put(StateEvidenceKind.MARKET, snapshot)

    def put_feature(self, snapshot: FeatureSnapshot) -> str:
        return self._put(StateEvidenceKind.FEATURE, snapshot)

    def put_derivative(self, snapshot: DerivativeContextSnapshot) -> str:
        return self._put(StateEvidenceKind.DERIVATIVE, snapshot)

    def put_account(self, snapshot: AccountSnapshot) -> str:
        return self._put(StateEvidenceKind.ACCOUNT, snapshot)

    def put_intelligence(self, event: IntelligenceEvent) -> str:
        return self._put(StateEvidenceKind.INTELLIGENCE, event)

    def get(
        self,
        evidence_ref: str,
    ) -> tuple[StateEvidenceKind, StateEvidence] | None:
        return self.get_many((evidence_ref,)).get(evidence_ref)

    def get_many(
        self,
        evidence_refs: tuple[str, ...],
    ) -> dict[str, tuple[StateEvidenceKind, StateEvidence]]:
        if len(set(evidence_refs)) != len(evidence_refs):
            raise ValueError("State evidence refs 不得重复")
        if not evidence_refs:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    state_evidence_snapshots.c.evidence_ref,
                    state_evidence_snapshots.c.evidence_kind,
                    state_evidence_snapshots.c.payload,
                ).where(state_evidence_snapshots.c.evidence_ref.in_(evidence_refs))
            ).all()
        result: dict[str, tuple[StateEvidenceKind, StateEvidence]] = {}
        for row in rows:
            kind = StateEvidenceKind(row.evidence_kind)
            result[row.evidence_ref] = (kind, _parse_evidence(kind, row.payload))
        return result

    def _put(self, kind: StateEvidenceKind, snapshot: StateEvidence) -> str:
        evidence_ref = content_hash(snapshot)
        if isinstance(
            snapshot,
            (MarketSnapshot, DerivativeContextSnapshot, AccountSnapshot, IntelligenceEvent),
        ):
            observed_at = snapshot.observed_at
        else:
            observed_at = snapshot.as_of
        as_of = (
            snapshot.observed_at
            if isinstance(snapshot, IntelligenceEvent)
            else snapshot.as_of
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(state_evidence_snapshots).values(
                        evidence_ref=evidence_ref,
                        evidence_kind=kind.value,
                        as_of=as_of,
                        observed_at=observed_at,
                        payload=snapshot.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            existing = self.get(evidence_ref)
            if existing != (kind, snapshot):
                raise ValueError("State evidence 内容哈希对应不同对象") from None
        return evidence_ref


def _parse_evidence(kind: StateEvidenceKind, payload: dict) -> StateEvidence:
    if kind == StateEvidenceKind.MARKET:
        return MarketSnapshot.model_validate(payload)
    if kind == StateEvidenceKind.FEATURE:
        return FeatureSnapshot.model_validate(payload)
    if kind == StateEvidenceKind.DERIVATIVE:
        return DerivativeContextSnapshot.model_validate(payload)
    if kind == StateEvidenceKind.INTELLIGENCE:
        return IntelligenceEvent.model_validate(payload)
    return AccountSnapshot.model_validate(payload)
