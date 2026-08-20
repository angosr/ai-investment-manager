from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Engine

from quant_core.asset_management import CanonicalFactRevision, MaterialDelta, StateSnapshot
from quant_core.domain import _require_utc
from quant_core.fact_pipeline import (
    validate_fact_revision_identity,
    validate_material_delta_identity,
    validate_state_snapshot_identity,
)
from quant_core.persistence import (
    canonical_fact_revision_sources,
    canonical_fact_revisions,
    material_deltas,
    source_observations,
    state_evidence_snapshots,
    state_snapshots,
)
from quant_core.sql_locking import advisory_xact_lock


class SqlFactStateStore:
    """Append-only fact revisions and point-in-time state transitions."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put_fact(self, fact: CanonicalFactRevision) -> CanonicalFactRevision:
        validate_fact_revision_identity(fact)
        with self._engine.begin() as connection:
            advisory_xact_lock(connection, "canonical_fact", fact.fact_id)
            latest_payload = connection.execute(
                select(canonical_fact_revisions.c.payload)
                .where(canonical_fact_revisions.c.fact_id == fact.fact_id)
                .order_by(canonical_fact_revisions.c.observed_at.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if latest_payload is not None:
                latest = CanonicalFactRevision.model_validate(latest_payload)
                if latest.revision_id == fact.revision_id:
                    if latest != fact:
                        raise ValueError("CanonicalFactRevision 身份对应不同内容")
                    return latest
                if latest.observed_at >= fact.observed_at:
                    raise ValueError("CanonicalFactRevision observed_at 必须严格递增")
                if fact.previous_revision_id != latest.revision_id:
                    raise ValueError("CanonicalFactRevision predecessor 不是当前最新修订")
            elif fact.previous_revision_id is not None:
                raise ValueError("首个 CanonicalFactRevision 不能引用 predecessor")

            self._require_source_observations(
                connection,
                fact.source_observation_ids,
                visible_at=fact.observed_at,
            )
            connection.execute(
                insert(canonical_fact_revisions).values(
                    revision_id=fact.revision_id,
                    fact_id=fact.fact_id,
                    previous_revision_id=fact.previous_revision_id,
                    projection_version=fact.projection_version,
                    status=fact.status.value,
                    observed_at=fact.observed_at,
                    revision_hash=fact.revision_hash,
                    payload=fact.model_dump(mode="json"),
                )
            )
            connection.execute(
                insert(canonical_fact_revision_sources),
                tuple(
                    {
                        "revision_id": fact.revision_id,
                        "observation_id": observation_id,
                    }
                    for observation_id in fact.source_observation_ids
                ),
            )
        return fact

    def fact(self, revision_id: str) -> CanonicalFactRevision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(canonical_fact_revisions.c.payload).where(
                    canonical_fact_revisions.c.revision_id == revision_id
                )
            ).scalar_one_or_none()
        return None if payload is None else CanonicalFactRevision.model_validate(payload)

    def latest_fact(self, fact_id: str) -> CanonicalFactRevision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(canonical_fact_revisions.c.payload)
                .where(canonical_fact_revisions.c.fact_id == fact_id)
                .order_by(canonical_fact_revisions.c.observed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else CanonicalFactRevision.model_validate(payload)

    def facts_as_of(self, *, as_of: datetime) -> tuple[CanonicalFactRevision, ...]:
        as_of = _require_utc(as_of)
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(canonical_fact_revisions.c.payload)
                .where(canonical_fact_revisions.c.observed_at <= as_of)
                .order_by(
                    canonical_fact_revisions.c.fact_id,
                    canonical_fact_revisions.c.observed_at.desc(),
                )
            ).scalars()
            latest: dict[str, CanonicalFactRevision] = {}
            for payload in payloads:
                fact = CanonicalFactRevision.model_validate(payload)
                latest.setdefault(fact.fact_id, fact)
        return tuple(sorted(latest.values(), key=lambda item: item.fact_id))

    def put_state(self, state: StateSnapshot) -> StateSnapshot:
        validate_state_snapshot_identity(state)
        with self._engine.begin() as connection:
            advisory_xact_lock(
                connection,
                "state_snapshot",
                state.analysis_scope,
                state.projection_version,
            )
            return self._put_state(connection, state)

    def record_transition(
        self,
        *,
        state: StateSnapshot,
        delta: MaterialDelta,
    ) -> tuple[StateSnapshot, MaterialDelta]:
        validate_state_snapshot_identity(state)
        validate_material_delta_identity(delta)
        if delta.previous_state_id is None:
            raise ValueError("持久化 MaterialDelta 必须引用 previous StateSnapshot")
        if delta.current_state_id != state.state_id:
            raise ValueError("MaterialDelta current_state_id 与 StateSnapshot 不一致")
        with self._engine.begin() as connection:
            advisory_xact_lock(
                connection,
                "state_snapshot",
                state.analysis_scope,
                state.projection_version,
            )
            replay_payload = connection.execute(
                select(material_deltas.c.payload).where(
                    material_deltas.c.delta_id == delta.delta_id
                )
            ).scalar_one_or_none()
            if replay_payload is not None:
                replayed_delta = MaterialDelta.model_validate(replay_payload)
                replayed_state_payload = connection.execute(
                    select(state_snapshots.c.payload).where(
                        state_snapshots.c.state_id == state.state_id
                    )
                ).scalar_one_or_none()
                if replayed_state_payload is None or replayed_delta != delta:
                    raise ValueError("MaterialDelta 幂等重放身份对应不同内容")
                replayed_state = StateSnapshot.model_validate(replayed_state_payload)
                if replayed_state.content_hash != state.content_hash:
                    raise ValueError("StateSnapshot 幂等重放身份对应不同内容")
                return replayed_state, replayed_delta
            latest_state_id = connection.execute(
                select(state_snapshots.c.state_id)
                .where(
                    state_snapshots.c.analysis_scope == state.analysis_scope,
                    state_snapshots.c.projection_version == state.projection_version,
                )
                .order_by(state_snapshots.c.as_of.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if latest_state_id != delta.previous_state_id:
                raise ValueError(
                    "MaterialDelta previous_state_id 不是 scope 当前最新状态"
                )
            recorded_state = self._put_state(connection, state)
            recorded_delta = self._put_delta(connection, delta)
        return recorded_state, recorded_delta

    def state(self, state_id: str) -> StateSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(state_snapshots.c.payload).where(
                    state_snapshots.c.state_id == state_id
                )
            ).scalar_one_or_none()
        return None if payload is None else StateSnapshot.model_validate(payload)

    def latest_state(
        self,
        *,
        analysis_scope: str,
        projection_version: str,
        as_of: datetime,
    ) -> StateSnapshot | None:
        as_of = _require_utc(as_of)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(state_snapshots.c.payload)
                .where(
                    state_snapshots.c.analysis_scope == analysis_scope,
                    state_snapshots.c.projection_version == projection_version,
                    state_snapshots.c.as_of <= as_of,
                )
                .order_by(state_snapshots.c.as_of.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else StateSnapshot.model_validate(payload)

    def delta(self, delta_id: str) -> MaterialDelta | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(material_deltas.c.payload).where(
                    material_deltas.c.delta_id == delta_id
                )
            ).scalar_one_or_none()
        return None if payload is None else MaterialDelta.model_validate(payload)

    @staticmethod
    def _require_source_observations(
        connection: Connection,
        observation_ids: tuple[str, ...],
        *,
        visible_at: datetime,
    ) -> None:
        existing = frozenset(
            connection.execute(
                select(source_observations.c.observation_id).where(
                    source_observations.c.observation_id.in_(observation_ids),
                    source_observations.c.observed_at <= visible_at,
                )
            ).scalars()
        )
        missing = tuple(sorted(set(observation_ids) - existing))
        if missing:
            raise ValueError(
                "CanonicalFactRevision 缺少截至 observed_at 可见的来源观测: "
                + ", ".join(missing)
            )

    @staticmethod
    def _put_state(connection: Connection, state: StateSnapshot) -> StateSnapshot:
        existing_payload = connection.execute(
            select(state_snapshots.c.payload).where(
                state_snapshots.c.analysis_scope == state.analysis_scope,
                state_snapshots.c.projection_version == state.projection_version,
                state_snapshots.c.as_of == state.as_of,
            )
        ).scalar_one_or_none()
        if existing_payload is not None:
            existing = StateSnapshot.model_validate(existing_payload)
            if existing.state_id != state.state_id:
                raise ValueError("同一 scope/projection/as_of 已存在不同 StateSnapshot")
            return existing

        if state.fact_revision_ids:
            visible_rows = connection.execute(
                select(
                    canonical_fact_revisions.c.revision_id,
                    canonical_fact_revisions.c.fact_id,
                ).where(
                    canonical_fact_revisions.c.revision_id.in_(
                        state.fact_revision_ids
                    ),
                    canonical_fact_revisions.c.observed_at <= state.as_of,
                )
            ).all()
            visible = {row.revision_id: row.fact_id for row in visible_rows}
            missing = tuple(sorted(set(state.fact_revision_ids) - set(visible)))
            if missing:
                raise ValueError(
                    "StateSnapshot 缺少截至 as_of 可见的事实修订: "
                    + ", ".join(missing)
                )
            latest_rows = connection.execute(
                select(
                    canonical_fact_revisions.c.revision_id,
                    canonical_fact_revisions.c.fact_id,
                )
                .where(
                    canonical_fact_revisions.c.fact_id.in_(set(visible.values())),
                    canonical_fact_revisions.c.observed_at <= state.as_of,
                )
                .order_by(
                    canonical_fact_revisions.c.fact_id,
                    canonical_fact_revisions.c.observed_at.desc(),
                )
            ).all()
            latest_by_fact: dict[str, str] = {}
            for row in latest_rows:
                latest_by_fact.setdefault(row.fact_id, row.revision_id)
            stale = tuple(
                sorted(
                    revision_id
                    for revision_id, fact_id in visible.items()
                    if latest_by_fact[fact_id] != revision_id
                )
            )
            if stale:
                raise ValueError(
                    "StateSnapshot 引用了截至 as_of 已过期的事实修订: "
                    + ", ".join(stale)
                )

        SqlFactStateStore._require_state_evidence(connection, state)

        connection.execute(
            insert(state_snapshots).values(
                state_id=state.state_id,
                projection_version=state.projection_version,
                analysis_scope=state.analysis_scope,
                as_of=state.as_of,
                built_at=state.built_at,
                content_hash=state.content_hash,
                payload=state.model_dump(mode="json"),
            )
        )
        return state

    @staticmethod
    def _require_state_evidence(
        connection: Connection,
        state: StateSnapshot,
    ) -> None:
        expected_kind = {
            **{item: "MARKET" for item in state.market_snapshot_refs},
            **{item: "FEATURE" for item in state.feature_snapshot_refs},
        }
        if state.account_snapshot_ref is not None:
            expected_kind[state.account_snapshot_ref] = "ACCOUNT"
        if not expected_kind:
            return
        rows = connection.execute(
            select(
                state_evidence_snapshots.c.evidence_ref,
                state_evidence_snapshots.c.evidence_kind,
            ).where(
                state_evidence_snapshots.c.evidence_ref.in_(expected_kind),
                state_evidence_snapshots.c.as_of <= state.as_of,
                state_evidence_snapshots.c.observed_at <= state.as_of,
            )
        ).all()
        visible = {row.evidence_ref: row.evidence_kind for row in rows}
        invalid = tuple(
            sorted(
                evidence_ref
                for evidence_ref, kind in expected_kind.items()
                if visible.get(evidence_ref) != kind
            )
        )
        if invalid:
            raise ValueError(
                "StateSnapshot 缺少截至 as_of 可见且类型匹配的 evidence: "
                + ", ".join(invalid)
            )

    @staticmethod
    def _put_delta(connection: Connection, delta: MaterialDelta) -> MaterialDelta:
        existing_payload = connection.execute(
            select(material_deltas.c.payload).where(
                material_deltas.c.delta_id == delta.delta_id
            )
        ).scalar_one_or_none()
        if existing_payload is not None:
            return MaterialDelta.model_validate(existing_payload)

        state_payloads = connection.execute(
            select(state_snapshots.c.state_id, state_snapshots.c.payload).where(
                state_snapshots.c.state_id.in_(
                    (delta.previous_state_id, delta.current_state_id)
                )
            )
        ).all()
        states = {
            row.state_id: StateSnapshot.model_validate(row.payload) for row in state_payloads
        }
        if set(states) != {delta.previous_state_id, delta.current_state_id}:
            raise ValueError("MaterialDelta 引用的 StateSnapshot 不完整")
        previous = states[delta.previous_state_id]
        current = states[delta.current_state_id]
        if previous.analysis_scope != delta.analysis_scope or current.analysis_scope != (
            delta.analysis_scope
        ):
            raise ValueError("MaterialDelta 与 StateSnapshot analysis_scope 不一致")
        if previous.projection_version != current.projection_version:
            raise ValueError("MaterialDelta 引用了不可比较的 StateSnapshot")
        if previous.as_of >= current.as_of or delta.observed_at > current.as_of:
            raise ValueError("MaterialDelta 与 StateSnapshot 时间顺序不一致")
        direct_predecessor_id = connection.execute(
            select(state_snapshots.c.state_id)
            .where(
                state_snapshots.c.analysis_scope == current.analysis_scope,
                state_snapshots.c.projection_version == current.projection_version,
                state_snapshots.c.as_of < current.as_of,
            )
            .order_by(state_snapshots.c.as_of.desc())
            .limit(1)
        ).scalar_one_or_none()
        if direct_predecessor_id != delta.previous_state_id:
            raise ValueError("MaterialDelta previous_state_id 不是 current 的直接前序状态")
        if not set(delta.fact_revision_ids).issubset(current.fact_revision_ids):
            raise ValueError("MaterialDelta 事实修订不属于 current StateSnapshot")
        if not set(delta.feature_snapshot_refs).issubset(current.feature_snapshot_refs):
            raise ValueError("MaterialDelta 特征引用不属于 current StateSnapshot")
        if set(delta.fact_revision_ids).issubset(previous.fact_revision_ids) and set(
            delta.feature_snapshot_refs
        ).issubset(previous.feature_snapshot_refs):
            raise ValueError("MaterialDelta 未引用相对 previous StateSnapshot 的新内容")

        connection.execute(
            insert(material_deltas).values(
                delta_id=delta.delta_id,
                policy_version=delta.policy_version,
                analysis_scope=delta.analysis_scope,
                previous_state_id=delta.previous_state_id,
                current_state_id=delta.current_state_id,
                category=delta.category.value,
                materiality=delta.materiality.value,
                observed_at=delta.observed_at,
                expires_at=delta.expires_at,
                content_hash=delta.content_hash,
                payload=delta.model_dump(mode="json"),
            )
        )
        return delta
