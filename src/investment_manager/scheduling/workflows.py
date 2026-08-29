from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from investment_manager.kernel.identity import stable_id
from investment_manager.platform.temporal import default_activity_versioning_intent
from investment_manager.scheduling.models import (
    AnalysisDispatchRequest,
    AnalysisTriggerEvent,
    AnalysisTriggerPlan,
    AnalysisTriggerType,
    TriggerBatch,
    TriggerOutboxKind,
    build_trigger_batch,
    build_trigger_event,
    select_trigger_batch_members,
    trigger_plan_accepts,
    trigger_reconsideration,
    trigger_rule_value,
)

BUILD_TRIGGER_DISPATCHES_ACTIVITY = "build-trigger-analysis-dispatches-v2"
TRIGGER_SIGNAL = "deliver-trigger-outbox-v1"


@workflow.defn(name="TriggerCoordinatorWorkflow")
class TriggerCoordinatorWorkflow:
    """每个 symbol/pipeline 唯一；只持有触发编排状态，不保存新闻正文。"""

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}
        self._plan: dict[str, Any] | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._prestart_triggers: dict[str, dict[str, Any]] = {}
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._consumed_wakeups: set[str] = set()
        self._signal_sequence = 0
        self._last_analysis_at: datetime | None = None
        self._completed_batches = 0
        self._failed_batches = 0
        self._unresolved_failure = False
        self._stopping = False
        self._last_batch_id: str | None = None
        self._active_batch_id: str | None = None
        self._frozen_retry_batch: dict[str, Any] | None = None
        self._input_retry_not_before: datetime | None = None
        self._next_reconsider_at: datetime | None = None

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "plan_revision": self._plan.get("revision") if self._plan else None,
            "pending_count": len(self._pending),
            "completed_batches": self._completed_batches,
            "failed_batches": self._failed_batches,
            "unresolved_failure": self._unresolved_failure,
            "last_batch_id": self._last_batch_id,
            "active_batch_id": self._active_batch_id,
            "next_reconsider_at": (
                self._next_reconsider_at.isoformat()
                if self._next_reconsider_at is not None
                else None
            ),
            "last_analysis_at": (
                self._last_analysis_at.isoformat() if self._last_analysis_at else None
            ),
        }

    @workflow.signal(name=TRIGGER_SIGNAL)
    def deliver(self, message: dict[str, Any]) -> None:
        kind = message.get("kind")
        if kind == TriggerOutboxKind.PLAN_REVISED.value:
            raw_plan = message.get("plan")
            if not isinstance(raw_plan, dict):
                return
            revision = raw_plan.get("revision")
            current_revision = self._plan.get("revision") if self._plan else 0
            if isinstance(revision, int) and revision > current_revision:
                self._plan = raw_plan
                self._signal_sequence += 1
            return
        if kind != TriggerOutboxKind.TRIGGER_CREATED.value:
            return
        raw_trigger = message.get("trigger")
        if not isinstance(raw_trigger, dict):
            return
        trigger_id = raw_trigger.get("trigger_id")
        if (
            not isinstance(trigger_id, str)
            or trigger_id in self._seen
            or trigger_id in self._prestart_triggers
        ):
            return
        if self._plan is None:
            self._prestart_triggers[trigger_id] = raw_trigger
            self._signal_sequence += 1
            return
        self._accept_trigger(raw_trigger)

    def _accept_trigger(self, raw_trigger: dict[str, Any]) -> None:
        assert self._plan is not None
        trigger_id = raw_trigger.get("trigger_id")
        if not isinstance(trigger_id, str) or trigger_id in self._seen:
            return
        if (
            raw_trigger.get("symbol") != self._plan.get("symbol")
            or raw_trigger.get("pipeline_id") != self._plan.get("pipeline_id")
            or not self._accepts(raw_trigger)
        ):
            return
        self._remember(trigger_id)
        self._pending[trigger_id] = raw_trigger
        self._signal_sequence += 1

    @workflow.signal
    def stop(self) -> None:
        self._stopping = True
        self._signal_sequence += 1

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._settings = request["settings"]
        state = request.get("runtime_state") or {}
        self._last_analysis_at = _parse_optional_time(state.get("last_analysis_at"))
        self._consumed_wakeups = set(state.get("consumed_wakeups", []))
        signal_seen = tuple(self._seen_order)
        self._seen_order = list(dict.fromkeys((*state.get("seen_trigger_ids", []), *signal_seen)))
        self._seen = set(self._seen_order)
        self._completed_batches = int(state.get("completed_batches", 0))
        self._failed_batches = int(state.get("failed_batches", 0))
        self._unresolved_failure = bool(state.get("unresolved_failure", False))
        initial_plan = request["plan"]
        current_revision = self._plan.get("revision") if self._plan else 0
        if int(initial_plan.get("revision", 0)) > int(current_revision):
            self._plan = initial_plan
        assert self._plan is not None
        prestart = tuple(self._prestart_triggers.values())
        self._prestart_triggers.clear()
        for raw_trigger in prestart:
            self._accept_trigger(raw_trigger)
        started_at = workflow.now()

        while not self._stopping:
            now = workflow.now()
            self._enqueue_due(now, started_at)
            self._trim_pending()
            self._discard_expired(now)
            if self._frozen_retry_batch is not None:
                batch = TriggerBatch.model_validate(self._frozen_retry_batch)
                if now >= batch.deadline:
                    self._fail_batch(batch, failed_at=now)
                    continue
                retry_at = self._input_retry_not_before or now
                self._next_reconsider_at = retry_at
                if retry_at > now:
                    await self._wait_for_change(retry_at - now)
                    continue
                selected_ids = tuple(item.trigger_id for item in batch.triggers)
            else:
                eligible = self._eligible_pending()
                if not eligible:
                    self._next_reconsider_at = None
                    await self._wait_for_change(self._next_timer_delay(now, started_at))
                    continue
                delay = self._required_delay(eligible, now)
                self._next_reconsider_at = now + delay
                if delay > timedelta(0):
                    await self._wait_for_change(delay)
                    continue
                self._next_reconsider_at = None
                selected = select_trigger_batch_members(
                    eligible,
                    maximum_batch_size=int(self._settings["maximum_batch_size"]),
                )
                plan = AnalysisTriggerPlan.model_validate(self._plan)
                triggers = tuple(
                    AnalysisTriggerEvent.model_validate(item)
                    for item in sorted(selected, key=lambda item: str(item["trigger_id"]))
                )
                batch = build_trigger_batch(
                    plan=plan,
                    triggers=triggers,
                    created_at=now,
                    deadline=now
                    + timedelta(seconds=int(self._settings["analysis_deadline_seconds"])),
                )
                selected_ids = tuple(item.trigger_id for item in triggers)
                self._active_batch_id = batch.batch_id
            (
                dispatches,
                deferred_until,
                retry_frozen_batch,
                permanent_failure,
            ) = await self._build_dispatches(batch.model_dump(mode="json"))
            if permanent_failure:
                self._fail_batch(batch, failed_at=workflow.now())
                continue
            if dispatches is None:
                if retry_frozen_batch:
                    self._frozen_retry_batch = batch.model_dump(mode="json")
                    self._active_batch_id = batch.batch_id
                else:
                    self._frozen_retry_batch = None
                    self._active_batch_id = None
                self._input_retry_not_before = deferred_until or (
                    workflow.now() + timedelta(seconds=int(self._settings["retry_maximum_seconds"]))
                )
                continue
            self._frozen_retry_batch = None
            self._input_retry_not_before = None
            for trigger_id in selected_ids:
                self._pending.pop(trigger_id, None)
            self._last_batch_id = batch.batch_id
            try:
                results = await asyncio.gather(
                    *(self._execute_dispatch(item) for item in dispatches)
                )
                if not all(results):
                    self._failed_batches += 1
                    self._unresolved_failure = True
                else:
                    self._unresolved_failure = False
            finally:
                self._active_batch_id = None
            completed_at = workflow.now()
            self._last_analysis_at = completed_at
            self._completed_batches += 1
            if self._completed_batches % 500 == 0 and not self._pending:
                workflow.continue_as_new(self._continued_request(request))

        return {
            "status": "STOPPED",
            "completed_batches": self._completed_batches,
            "last_batch_id": self._last_batch_id,
        }

    async def _build_dispatches(
        self,
        batch: dict[str, Any],
    ) -> tuple[
        tuple[AnalysisDispatchRequest, ...] | None,
        datetime | None,
        bool,
        bool,
    ]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=int(self._settings["retry_initial_seconds"])),
            maximum_interval=timedelta(seconds=int(self._settings["retry_maximum_seconds"])),
            backoff_coefficient=float(self._settings["retry_backoff_coefficient"]),
            maximum_attempts=int(self._settings["retry_maximum_attempts"]),
            non_retryable_error_types=["InvalidTriggerBatch", "PermanentDomainError"],
        )
        try:
            result = await workflow.execute_activity(
                BUILD_TRIGGER_DISPATCHES_ACTIVITY,
                batch,
                result_type=dict,
                start_to_close_timeout=timedelta(
                    seconds=int(self._settings["activity_start_to_close_seconds"])
                ),
                schedule_to_close_timeout=timedelta(
                    seconds=int(self._settings["activity_schedule_to_close_seconds"])
                ),
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="冻结触发批次分析输入",
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type in {
                "InvalidTriggerBatch",
                "PermanentDomainError",
            }:
                return None, None, False, True
            return None, None, False, False
        retry_frozen_batch = result.get("retry_frozen_batch") is True
        deferred_until = result.get("deferred_until")
        if isinstance(deferred_until, str):
            return None, _parse_time(deferred_until), retry_frozen_batch, False
        if retry_frozen_batch:
            return None, None, True, False
        raw_dispatches = result.get("workflow_dispatches")
        if not isinstance(raw_dispatches, list):
            return None, None, False, True
        try:
            dispatches = tuple(
                AnalysisDispatchRequest.model_validate(item) for item in raw_dispatches
            )
        except (TypeError, ValueError):
            return None, None, False, True
        return dispatches, None, False, False

    @staticmethod
    async def _execute_dispatch(dispatch: AnalysisDispatchRequest) -> bool:
        try:
            result = await workflow.execute_child_workflow(
                dispatch.workflow_name,
                dispatch.payload,
                id=dispatch.workflow_id,
                task_queue=dispatch.task_queue,
                result_type=dict,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                static_summary="执行事件驱动分析任务",
            )
        except ChildWorkflowError:
            return False
        # Child workflows use FAILED only when durable orchestration could not
        # produce a business terminal (for example Forecast or NO_ESTIMATE).
        # A normally returned FAILED must therefore remain visible in coordinator
        # health just like an exception; business terminals such as NO_ESTIMATE
        # and NO_ASSESSMENT are successful dispatch completion.
        status = result.get("status") if isinstance(result, dict) else None
        return isinstance(status, str) and status != "FAILED"

    def _accepts(self, trigger: dict[str, Any]) -> bool:
        assert self._plan is not None
        return trigger_plan_accepts(self._plan, trigger)

    def _eligible_pending(self) -> list[dict[str, Any]]:
        if self._plan is None:
            return []
        paused = bool(self._plan.get("ai_paused"))
        items = [
            item
            for item in self._pending.values()
            if not paused or item.get("trigger_type") == AnalysisTriggerType.AGENT_WAKEUP.value
        ]
        return sorted(
            items,
            key=lambda item: (
                -int(item.get("priority", 0)),
                str(item.get("observed_at", "")),
                str(item.get("trigger_id", "")),
            ),
        )

    def _required_delay(self, pending: list[dict[str, Any]], now: datetime) -> timedelta:
        assert self._plan is not None
        timing = trigger_reconsideration(
            plan=self._plan,
            pending=pending,
            now=now,
            last_analysis_at=self._last_analysis_at,
            input_retry_not_before=self._input_retry_not_before,
            wake_at_expiry=workflow.patched("pending-expiry-wakeup-v1"),
        )
        return timing.reconsider_at - now

    def _rule_value(self, trigger: dict[str, Any], field: str) -> int:
        assert self._plan is not None
        if field not in {"coalesce_seconds", "ordinary_cooldown_seconds"}:
            raise ValueError("未知 TriggerPlan 时间字段")
        return trigger_rule_value(self._plan, trigger, field)

    def _enqueue_due(self, now: datetime, started_at: datetime) -> None:
        if self._plan is None or bool(self._plan.get("ai_paused")):
            return
        for wakeup in self._plan.get("scheduled_wakeups", []):
            key = f"{wakeup['wakeup_id']}:{wakeup['wake_at']}"
            if key in self._consumed_wakeups or _parse_time(wakeup["wake_at"]) > now:
                continue
            self._consumed_wakeups.add(key)
            if _parse_time(wakeup["expires_at"]) <= now:
                continue
            trigger = build_trigger_event(
                trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
                symbol=str(self._plan["symbol"]),
                pipeline_id=str(self._plan["pipeline_id"]),
                occurred_at=_parse_time(wakeup["wake_at"]),
                observed_at=now,
                priority=90,
                dedup_key=key,
                evidence_ids=tuple(wakeup.get("evidence_ids", [])),
                review_reason=str(wakeup["reason"]),
                expires_at=_parse_time(wakeup["expires_at"]),
                plan_revision=int(self._plan["revision"]),
            )
            self._remember(trigger.trigger_id)
            self._pending[trigger.trigger_id] = trigger.model_dump(mode="json")
        heartbeat = self._plan.get("heartbeat_seconds")
        anchor = self._last_analysis_at
        if anchor is None:
            anchor = (
                started_at - timedelta(seconds=int(heartbeat))
                if heartbeat is not None and workflow.patched("immediate-initial-heartbeat-v1")
                else started_at
            )
        if heartbeat is not None and now >= anchor + timedelta(seconds=int(heartbeat)):
            dedup_key = f"{int(anchor.timestamp())}:{int(heartbeat)}"
            durable_heartbeat = workflow.patched("durable-heartbeat-v1")
            trigger = build_trigger_event(
                trigger_type=AnalysisTriggerType.HEARTBEAT,
                symbol=str(self._plan["symbol"]),
                pipeline_id=str(self._plan["pipeline_id"]),
                occurred_at=anchor + timedelta(seconds=int(heartbeat)),
                observed_at=now,
                priority=10,
                dedup_key=dedup_key,
                expires_at=(
                    None
                    if durable_heartbeat
                    else now + timedelta(seconds=int(self._settings["trigger_expiry_seconds"]))
                ),
                plan_revision=int(self._plan["revision"]),
            )
            unseen = trigger.trigger_id not in self._seen
            if unseen:
                self._remember(trigger.trigger_id)
            if trigger.trigger_id not in self._pending and (durable_heartbeat or unseen):
                self._pending[trigger.trigger_id] = trigger.model_dump(mode="json")

    def _discard_expired(self, now: datetime) -> None:
        protected = (
            {
                str(item["trigger_id"])
                for item in self._frozen_retry_batch.get("triggers", [])
                if isinstance(item, dict) and isinstance(item.get("trigger_id"), str)
            }
            if self._frozen_retry_batch is not None
            else set()
        )
        self._pending = {
            key: item
            for key, item in self._pending.items()
            if key in protected
            or item.get("expires_at") is None
            or _parse_time(item["expires_at"]) > now
        }

    def _fail_batch(self, batch: TriggerBatch, *, failed_at: datetime) -> None:
        for trigger in batch.triggers:
            self._pending.pop(trigger.trigger_id, None)
        self._last_batch_id = batch.batch_id
        self._failed_batches += 1
        self._unresolved_failure = True
        # A terminal heartbeat failure is consumed, not immediately regenerated
        # from the previous successful anchor.  The next heartbeat is a fresh
        # recovery opportunity after the normal interval.
        self._last_analysis_at = failed_at
        self._active_batch_id = None
        self._frozen_retry_batch = None
        self._input_retry_not_before = None
        self._next_reconsider_at = None

    def _trim_pending(self) -> None:
        maximum = int(self._settings["maximum_pending_triggers"])
        if len(self._pending) <= maximum:
            return
        ordered = sorted(
            self._pending.values(),
            key=lambda item: (
                -int(item.get("priority", 0)),
                str(item.get("observed_at", "")),
                str(item.get("trigger_id", "")),
            ),
        )
        self._pending = {item["trigger_id"]: item for item in ordered[:maximum]}

    def _next_timer_delay(self, now: datetime, started_at: datetime) -> timedelta:
        if self._plan is None or bool(self._plan.get("ai_paused")):
            return timedelta(days=1)
        candidates = [
            _parse_time(item["wake_at"])
            for item in self._plan.get("scheduled_wakeups", [])
            if f"{item['wakeup_id']}:{item['wake_at']}" not in self._consumed_wakeups
        ]
        heartbeat = self._plan.get("heartbeat_seconds")
        if heartbeat is not None:
            candidates.append(
                (self._last_analysis_at or started_at) + timedelta(seconds=int(heartbeat))
            )
        if not candidates:
            return timedelta(days=1)
        return max(min(candidates) - now, timedelta(0))

    async def _wait_for_change(self, timeout: timedelta) -> None:
        sequence = self._signal_sequence
        with suppress(TimeoutError):
            await workflow.wait_condition(
                lambda: self._signal_sequence != sequence or self._stopping,
                timeout=max(timeout, timedelta(milliseconds=1)),
            )

    def _remember(self, trigger_id: str) -> None:
        self._seen.add(trigger_id)
        self._seen_order.append(trigger_id)
        if len(self._seen_order) > 5000:
            removed = self._seen_order.pop(0)
            self._seen.discard(removed)

    def _continued_request(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            **request,
            "plan": self._plan,
            "runtime_state": {
                "last_analysis_at": (
                    self._last_analysis_at.isoformat() if self._last_analysis_at else None
                ),
                "consumed_wakeups": sorted(self._consumed_wakeups),
                "seen_trigger_ids": self._seen_order,
                "completed_batches": self._completed_batches,
                "failed_batches": self._failed_batches,
                "unresolved_failure": self._unresolved_failure,
            },
        }


def coordinator_workflow_id(symbol: str, pipeline_id: str) -> str:
    return stable_id("trigger_coordinator", symbol, pipeline_id)


def _parse_time(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _parse_optional_time(value: str | datetime | None) -> datetime | None:
    return _parse_time(value) if value is not None else None
