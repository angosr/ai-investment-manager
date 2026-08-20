from __future__ import annotations

from datetime import datetime

from pydantic import field_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.config import TriggerPolicy
from investment_manager.domain import FrozenModel, _require_utc
from investment_manager.kernel.identity import stable_id
from investment_manager.persistence import (
    analysis_call_admissions,
    analysis_scheduled_wakeups,
    analysis_trigger_batches,
    analysis_trigger_events,
    analysis_trigger_plans,
    insert_trigger_with_outbox,
    notify_trigger_outbox,
    trigger_outbox,
)
from investment_manager.sql_time import database_utc
from investment_manager.trigger import (
    AnalysisCallAdmission,
    AnalysisTriggerEvent,
    AnalysisTriggerPlan,
    TriggerBatch,
    TriggerOutboxKind,
    TriggerPlanApplyResult,
    TriggerPlanGate,
    TriggerPlanPatch,
    decide_analysis_call_admission,
)


class TriggerOutboxMessage(FrozenModel):
    outbox_id: str
    aggregate_key: str
    message_kind: TriggerOutboxKind
    created_at: datetime
    available_at: datetime
    attempt_count: int
    payload: dict

    _utc_created_at = field_validator("created_at")(_require_utc)
    _utc_available_at = field_validator("available_at")(_require_utc)


class SqlTriggerRepository:
    """TriggerPlan 和 Outbox 的唯一写边界；事务之外不产生调度事实。"""

    def __init__(self, engine: Engine, policy: TriggerPolicy) -> None:
        self._engine = engine
        self._policy = policy
        self._gate = TriggerPlanGate(policy)

    def create_plan(self, plan: AnalysisTriggerPlan) -> AnalysisTriggerPlan:
        try:
            with self._engine.begin() as connection:
                self._insert_plan(connection, plan)
                self._insert_plan_outbox(connection, plan)
        except IntegrityError:
            existing = self.current_plan(plan.plan_id)
            if existing != plan:
                raise ValueError("AnalysisTriggerPlan 已存在且内容不同") from None
            return existing
        return plan

    def record_trigger(self, trigger: AnalysisTriggerEvent) -> bool:
        try:
            with self._engine.begin() as connection:
                insert_trigger_with_outbox(connection, trigger)
        except IntegrityError:
            with self._engine.connect() as connection:
                payload = connection.execute(
                    select(analysis_trigger_events.c.payload).where(
                        analysis_trigger_events.c.trigger_id == trigger.trigger_id
                    )
                ).scalar_one_or_none()
            if payload is None or AnalysisTriggerEvent.model_validate(payload) != trigger:
                raise ValueError("AnalysisTriggerEvent 唯一键冲突且事实不一致") from None
            return False
        return True

    def record_batch(self, batch: TriggerBatch, *, analysis_submitted_at: datetime) -> bool:
        analysis_submitted_at = _require_utc(analysis_submitted_at)
        payload = batch.model_dump(mode="json")
        values = {
            "batch_id": batch.batch_id,
            "symbol": batch.symbol,
            "pipeline_id": batch.pipeline_id,
            "plan_revision": batch.plan_revision,
            "first_occurred_at": min(item.occurred_at for item in batch.triggers),
            "first_observed_at": min(item.observed_at for item in batch.triggers),
            "batched_at": batch.created_at,
            "analysis_submitted_at": analysis_submitted_at,
            "payload": payload,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(analysis_trigger_batches).values(**values))
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(analysis_trigger_batches.c.payload).where(
                        analysis_trigger_batches.c.batch_id == batch.batch_id
                    )
                ).scalar_one_or_none()
            if existing != payload:
                raise ValueError("TriggerBatch 已存在且事实不一致") from None
            return False
        return True

    def admit_analysis_call(
        self,
        batch: TriggerBatch,
        *,
        requested_at: datetime,
    ) -> AnalysisCallAdmission:
        """跨品种原子防重复；同一批次重试不重复计数。"""

        requested_at = _require_utc(requested_at)
        with self._engine.begin() as connection:
            if self._engine.dialect.name == "postgresql":
                # 与 Dispatcher 的会话级领导锁使用不同 key；xact 锁只保护短事务。
                connection.execute(
                    select(
                        func.pg_advisory_xact_lock(self._policy.dispatcher_advisory_lock_key + 1)
                    )
                ).scalar_one()
            existing = connection.execute(
                select(analysis_call_admissions.c.admitted_at).where(
                    analysis_call_admissions.c.batch_id == batch.batch_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return AnalysisCallAdmission(
                    admitted=True,
                    admitted_at=database_utc(existing),
                )

            latest = connection.execute(
                select(func.max(analysis_call_admissions.c.admitted_at)).where(
                    analysis_call_admissions.c.admitted_at <= requested_at
                )
            ).scalar_one()
            admission = decide_analysis_call_admission(
                requested_at=requested_at,
                last_admitted_at=database_utc(latest) if latest is not None else None,
                minimum_call_interval_seconds=self._policy.minimum_call_interval_seconds,
            )
            if not admission.admitted:
                return admission

            payload = {
                "batch_id": batch.batch_id,
                "pipeline_id": batch.pipeline_id,
                "symbol": batch.symbol,
                "admitted_at": requested_at.isoformat(),
                "trigger_policy_version": self._policy.version,
            }
            connection.execute(
                insert(analysis_call_admissions).values(
                    batch_id=batch.batch_id,
                    pipeline_id=batch.pipeline_id,
                    symbol=batch.symbol,
                    admitted_at=requested_at,
                    payload=payload,
                )
            )
        return AnalysisCallAdmission(admitted=True, admitted_at=requested_at)

    def current_plan(self, plan_id: str) -> AnalysisTriggerPlan:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(analysis_trigger_plans.c.payload).where(
                    analysis_trigger_plans.c.plan_id == plan_id,
                    analysis_trigger_plans.c.is_current.is_(True),
                )
            ).scalar_one_or_none()
        if payload is None:
            raise KeyError(plan_id)
        return AnalysisTriggerPlan.model_validate(payload)

    def plan_for_scope(self, *, symbol: str, pipeline_id: str) -> AnalysisTriggerPlan:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(analysis_trigger_plans.c.payload).where(
                    analysis_trigger_plans.c.symbol == symbol,
                    analysis_trigger_plans.c.pipeline_id == pipeline_id,
                    analysis_trigger_plans.c.is_current.is_(True),
                )
            ).scalar_one_or_none()
        if payload is None:
            raise KeyError(f"{symbol}:{pipeline_id}")
        return AnalysisTriggerPlan.model_validate(payload)

    def current_plans_for_symbols(
        self,
        symbols: tuple[str, ...],
    ) -> tuple[AnalysisTriggerPlan, ...]:
        """返回指定交易范围内各 pipeline 的当前 revision。"""

        if not symbols:
            return ()
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(analysis_trigger_plans.c.payload)
                .where(
                    analysis_trigger_plans.c.symbol.in_(symbols),
                    analysis_trigger_plans.c.is_current.is_(True),
                )
                .order_by(
                    analysis_trigger_plans.c.symbol,
                    analysis_trigger_plans.c.pipeline_id,
                )
            ).scalars()
            return tuple(AnalysisTriggerPlan.model_validate(payload) for payload in payloads)

    def apply_patch(
        self,
        patch: TriggerPlanPatch,
        *,
        now: datetime,
        current_manifest_id: str,
    ) -> TriggerPlanApplyResult:
        now = _require_utc(now)
        with self._engine.begin() as connection:
            replay = connection.execute(
                select(analysis_trigger_plans.c.payload).where(
                    analysis_trigger_plans.c.applied_patch_id == patch.patch_id
                )
            ).scalar_one_or_none()
            if replay is not None:
                return TriggerPlanApplyResult(plan=AnalysisTriggerPlan.model_validate(replay))
            current_payload = connection.execute(
                select(analysis_trigger_plans.c.payload)
                .where(
                    analysis_trigger_plans.c.plan_id == patch.plan_id,
                    analysis_trigger_plans.c.is_current.is_(True),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current_payload is None:
                raise KeyError(patch.plan_id)
            current = AnalysisTriggerPlan.model_validate(current_payload)
            result = self._gate.apply(
                current,
                patch,
                now=now,
                current_manifest_id=current_manifest_id,
            )
            changed = connection.execute(
                update(analysis_trigger_plans)
                .where(
                    analysis_trigger_plans.c.plan_id == current.plan_id,
                    analysis_trigger_plans.c.revision == current.revision,
                    analysis_trigger_plans.c.is_current.is_(True),
                )
                .values(is_current=False)
            )
            if changed.rowcount != 1:
                raise ValueError("AnalysisTriggerPlan 并发 revision 冲突")
            self._insert_plan(connection, result.plan)
            for trigger in result.emitted_triggers:
                insert_trigger_with_outbox(connection, trigger)
            self._insert_plan_outbox(connection, result.plan)
        return result

    def pending_outbox(
        self,
        *,
        as_of: datetime,
        limit: int = 100,
    ) -> tuple[TriggerOutboxMessage, ...]:
        as_of = _require_utc(as_of)
        if not 1 <= limit <= 1000:
            raise ValueError("Outbox batch limit 必须在 1..1000")
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(trigger_outbox)
                .where(
                    trigger_outbox.c.status == "PENDING",
                    trigger_outbox.c.available_at <= as_of,
                )
                .order_by(trigger_outbox.c.available_at, trigger_outbox.c.outbox_id)
                .limit(limit)
            ).mappings()
            return tuple(
                TriggerOutboxMessage(
                    outbox_id=row["outbox_id"],
                    aggregate_key=row["aggregate_key"],
                    message_kind=row["message_kind"],
                    created_at=database_utc(row["created_at"]),
                    available_at=database_utc(row["available_at"]),
                    attempt_count=row["attempt_count"],
                    payload=row["payload"],
                )
                for row in rows
            )

    def mark_delivered(self, outbox_id: str, *, delivered_at: datetime) -> None:
        delivered_at = _require_utc(delivered_at)
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(trigger_outbox.c.status).where(trigger_outbox.c.outbox_id == outbox_id)
            ).scalar_one_or_none()
            if existing is None:
                raise KeyError(outbox_id)
            if existing == "DELIVERED":
                return
            connection.execute(
                update(trigger_outbox)
                .where(trigger_outbox.c.outbox_id == outbox_id)
                .values(
                    status="DELIVERED",
                    delivered_at=delivered_at,
                    attempt_count=trigger_outbox.c.attempt_count + 1,
                )
            )

    def record_delivery_failure(self, outbox_id: str) -> None:
        with self._engine.begin() as connection:
            changed = connection.execute(
                update(trigger_outbox)
                .where(
                    trigger_outbox.c.outbox_id == outbox_id,
                    trigger_outbox.c.status == "PENDING",
                )
                .values(attempt_count=trigger_outbox.c.attempt_count + 1)
            )
            if changed.rowcount != 1:
                raise KeyError(outbox_id)

    def _insert_plan(self, connection: Connection, plan: AnalysisTriggerPlan) -> None:
        connection.execute(
            insert(analysis_trigger_plans).values(
                plan_id=plan.plan_id,
                revision=plan.revision,
                symbol=plan.symbol,
                pipeline_id=plan.pipeline_id,
                manifest_id=plan.manifest_id,
                is_current=True,
                applied_patch_id=plan.applied_patch_id,
                updated_at=plan.updated_at,
                payload=plan.model_dump(mode="json"),
            )
        )
        if plan.scheduled_wakeups:
            connection.execute(
                insert(analysis_scheduled_wakeups),
                [
                    {
                        "plan_id": plan.plan_id,
                        "plan_revision": plan.revision,
                        "wakeup_id": item.wakeup_id,
                        "wake_at": item.wake_at,
                        "expires_at": item.expires_at,
                        "payload": item.model_dump(mode="json"),
                    }
                    for item in plan.scheduled_wakeups
                ],
            )

    @staticmethod
    def _insert_plan_outbox(connection: Connection, plan: AnalysisTriggerPlan) -> None:
        outbox_id = stable_id(
            "trigger_outbox", TriggerOutboxKind.PLAN_REVISED.value, plan.plan_id, plan.revision
        )
        connection.execute(
            insert(trigger_outbox).values(
                outbox_id=outbox_id,
                aggregate_key=f"{plan.symbol}:{plan.pipeline_id}",
                message_kind=TriggerOutboxKind.PLAN_REVISED.value,
                status="PENDING",
                created_at=plan.updated_at,
                available_at=plan.updated_at,
                delivered_at=None,
                attempt_count=0,
                payload={
                    "kind": TriggerOutboxKind.PLAN_REVISED.value,
                    "plan": plan.model_dump(mode="json"),
                },
            )
        )
        notify_trigger_outbox(connection, f"{plan.symbol}:{plan.pipeline_id}")


class PostgresTriggerLeadership:
    """Dispatcher 单领导者锁；连接中断时由 PostgreSQL 自动释放。"""

    def __init__(self, engine: Engine, lock_key: int) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Trigger Dispatcher 领导锁只支持 PostgreSQL")
        self._engine = engine
        self._lock_key = lock_key
        self._connection: Connection | None = None

    def acquire(self) -> bool:
        if self._connection is not None:
            raise RuntimeError("领导锁已经持有")
        connection = self._engine.connect()
        acquired = bool(
            connection.execute(select(func.pg_try_advisory_lock(self._lock_key))).scalar_one()
        )
        if not acquired:
            connection.close()
            return False
        self._connection = connection
        return True

    def release(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute(select(func.pg_advisory_unlock(self._lock_key)))
        finally:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> PostgresTriggerLeadership:
        if not self.acquire():
            raise RuntimeError("已有 Trigger Dispatcher 持有领导锁")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class PostgresOutboxListener:
    """LISTEN 是延迟优化；连接或通知丢失时 Dispatcher 仍按 Outbox 回扫。"""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Outbox LISTEN 只支持 PostgreSQL")
        self._raw = engine.raw_connection()
        self._connection = self._raw.driver_connection
        self._previous_autocommit = self._connection.autocommit
        self._connection.autocommit = True
        self._connection.execute("LISTEN quant_trigger_outbox")

    def wait(self, timeout_seconds: float) -> bool:
        return bool(list(self._connection.notifies(timeout=timeout_seconds, stop_after=1)))

    def close(self) -> None:
        try:
            self._connection.execute("UNLISTEN quant_trigger_outbox")
            self._connection.autocommit = self._previous_autocommit
        except Exception:
            # 连接状态无法恢复时必须逐出连接池，不能让后续事务继承 autocommit。
            self._raw.invalidate()
        finally:
            self._raw.close()

    def __enter__(self) -> PostgresOutboxListener:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
