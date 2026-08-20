from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

from quant_core.ids import stable_id
from quant_core.temporal_compat import default_activity_versioning_intent
from quant_core.temporal_workflows import AnalysisCycleWorkflow
from quant_core.trigger import (
    AnalysisTriggerEvent,
    AnalysisTriggerPlan,
    AnalysisTriggerType,
    TriggerOutboxKind,
    build_trigger_batch,
    build_trigger_event,
    trigger_plan_accepts,
    trigger_reconsideration,
    trigger_rule_value,
)

BUILD_TRIGGER_REQUEST_ACTIVITY = "build-trigger-analysis-request-v1"
TRIGGER_SIGNAL = "deliver-trigger-outbox-v1"


@workflow.defn(name="TriggerCoordinatorWorkflow")
class TriggerCoordinatorWorkflow:
    """每个 symbol/pipeline 唯一；只持有触发编排状态，不保存新闻正文。"""

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}
        self._plan: dict[str, Any] | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._consumed_wakeups: set[str] = set()
        self._signal_sequence = 0
        self._last_analysis_at: datetime | None = None
        self._completed_batches = 0
        self._failed_batches = 0
        self._stopping = False
        self._last_batch_id: str | None = None
        self._active_batch_id: str | None = None
        self._input_retry_not_before: datetime | None = None
        self._next_reconsider_at: datetime | None = None

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "plan_revision": self._plan.get("revision") if self._plan else None,
            "pending_count": len(self._pending),
            "completed_batches": self._completed_batches,
            "failed_batches": self._failed_batches,
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
        if kind != TriggerOutboxKind.TRIGGER_CREATED.value or self._plan is None:
            return
        raw_trigger = message.get("trigger")
        if not isinstance(raw_trigger, dict):
            return
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
        maximum = int(self._settings["maximum_pending_triggers"])
        if len(self._pending) > maximum:
            ordered = sorted(
                self._pending.values(),
                key=lambda item: (
                    -int(item.get("priority", 0)),
                    str(item.get("observed_at", "")),
                    str(item.get("trigger_id", "")),
                ),
            )
            self._pending = {item["trigger_id"]: item for item in ordered[:maximum]}
        self._signal_sequence += 1

    @workflow.signal
    def stop(self) -> None:
        self._stopping = True
        self._signal_sequence += 1

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._settings = request["settings"]
        self._plan = request["plan"]
        state = request.get("runtime_state") or {}
        self._last_analysis_at = _parse_optional_time(state.get("last_analysis_at"))
        self._consumed_wakeups = set(state.get("consumed_wakeups", []))
        self._seen_order = list(state.get("seen_trigger_ids", []))
        self._seen = set(self._seen_order)
        self._completed_batches = int(state.get("completed_batches", 0))
        self._failed_batches = int(state.get("failed_batches", 0))
        started_at = workflow.now()

        while not self._stopping:
            now = workflow.now()
            self._enqueue_due(now, started_at)
            self._discard_expired(now)
            eligible = self._eligible_pending()
            if eligible:
                delay = self._required_delay(eligible, now)
                self._next_reconsider_at = now + delay
                if delay > timedelta(0):
                    await self._wait_for_change(delay)
                    continue
                self._next_reconsider_at = None
                maximum = int(self._settings["maximum_batch_size"])
                selected = tuple(eligible[:maximum])
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
                self._active_batch_id = batch.batch_id
                request_payload, deferred_until = await self._build_request(
                    batch.model_dump(mode="json")
                )
                if request_payload is None:
                    self._active_batch_id = None
                    self._input_retry_not_before = deferred_until or (
                        workflow.now()
                        + timedelta(seconds=int(self._settings["retry_maximum_seconds"]))
                    )
                    continue
                self._input_retry_not_before = None
                for item in selected:
                    self._pending.pop(str(item["trigger_id"]), None)
                self._last_batch_id = batch.batch_id
                try:
                    try:
                        await workflow.execute_child_workflow(
                            AnalysisCycleWorkflow.run,
                            request_payload,
                            id=str(request_payload["workflow_id"]),
                            task_queue=str(self._settings["analysis_task_queue"]),
                            result_type=dict,
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            static_summary="执行事件驱动分析批次",
                        )
                    except ChildWorkflowError:
                        self._failed_batches += 1
                finally:
                    self._active_batch_id = None
                completed_at = workflow.now()
                self._last_analysis_at = completed_at
                self._completed_batches += 1
                if self._completed_batches % 500 == 0 and not self._pending:
                    workflow.continue_as_new(self._continued_request(request))
                continue

            self._next_reconsider_at = None
            await self._wait_for_change(self._next_timer_delay(now, started_at))

        return {
            "status": "STOPPED",
            "completed_batches": self._completed_batches,
            "last_batch_id": self._last_batch_id,
        }

    async def _build_request(
        self,
        batch: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, datetime | None]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=int(self._settings["retry_initial_seconds"])),
            maximum_interval=timedelta(seconds=int(self._settings["retry_maximum_seconds"])),
            backoff_coefficient=float(self._settings["retry_backoff_coefficient"]),
            maximum_attempts=int(self._settings["retry_maximum_attempts"]),
            non_retryable_error_types=["InvalidTriggerBatch", "PermanentDomainError"],
        )
        try:
            result = await workflow.execute_activity(
                BUILD_TRIGGER_REQUEST_ACTIVITY,
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
        except ActivityError:
            return None, None
        deferred_until = result.get("deferred_until")
        if isinstance(deferred_until, str):
            return None, _parse_time(deferred_until)
        raw_request = result.get("workflow_request")
        return (raw_request, None) if isinstance(raw_request, dict) else (None, None)

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
                expires_at=_parse_time(wakeup["expires_at"]),
                plan_revision=int(self._plan["revision"]),
            )
            self._remember(trigger.trigger_id)
            self._pending[trigger.trigger_id] = trigger.model_dump(mode="json")
        heartbeat = self._plan.get("heartbeat_seconds")
        anchor = self._last_analysis_at or started_at
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
            if trigger.trigger_id not in self._pending and (
                durable_heartbeat or unseen
            ):
                self._pending[trigger.trigger_id] = trigger.model_dump(mode="json")

    def _discard_expired(self, now: datetime) -> None:
        self._pending = {
            key: item
            for key, item in self._pending.items()
            if item.get("expires_at") is None or _parse_time(item["expires_at"]) > now
        }

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
            },
        }


def coordinator_workflow_id(symbol: str, pipeline_id: str) -> str:
    return stable_id("trigger_coordinator", symbol, pipeline_id)


def _parse_time(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _parse_optional_time(value: str | datetime | None) -> datetime | None:
    return _parse_time(value) if value is not None else None
