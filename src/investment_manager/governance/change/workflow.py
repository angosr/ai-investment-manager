from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from investment_manager.platform.temporal import default_activity_versioning_intent

BUILD_GOVERNANCE_SNAPSHOT_ACTIVITY = "build-governance-snapshot-v1"
RUN_GOVERNOR_ACTIVITY = "run-governor-v1"


@workflow.defn(name="GovernanceCycleWorkflow")
class GovernanceCycleWorkflow:
    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(request.get("workflow_id") or workflow.info().workflow_id)
        raw_hash = request.get("input_hash")
        if not isinstance(raw_hash, str) or not raw_hash:
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")
        self._input_hash = raw_hash
        try:
            orchestration = request["orchestration"]
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=int(orchestration["retry_initial_seconds"])),
                maximum_interval=timedelta(seconds=int(orchestration["retry_maximum_seconds"])),
                backoff_coefficient=float(orchestration["retry_backoff_coefficient"]),
                maximum_attempts=int(orchestration["retry_maximum_attempts"]),
                non_retryable_error_types=["InvalidGovernanceInput"],
            )
            start_to_close = timedelta(
                seconds=int(orchestration["activity_start_to_close_seconds"])
            )
            schedule_to_close = timedelta(
                seconds=int(orchestration["activity_schedule_to_close_seconds"])
            )
        except (KeyError, TypeError, ValueError):
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")
        try:
            snapshot_result = await workflow.execute_activity(
                BUILD_GOVERNANCE_SNAPSHOT_ACTIVITY,
                request,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=schedule_to_close,
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="冻结最小治理信息面板",
            )
            snapshot = snapshot_result.get("snapshot")
            if not isinstance(snapshot, dict):
                return _failed(workflow_id, "INVALID_SNAPSHOT_RESULT")
            governor_result = await workflow.execute_activity(
                RUN_GOVERNOR_ACTIVITY,
                snapshot,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=schedule_to_close,
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="运行无历史上下文的受限 Governor",
            )
        except ActivityError:
            return _failed(workflow_id, "GOVERNANCE_ACTIVITY_FAILED")
        decision = governor_result.get("decision")
        if not isinstance(decision, dict):
            return _failed(
                workflow_id,
                str(governor_result.get("reason_code") or "GOVERNOR_FAILED"),
            )
        return {
            "workflow_id": workflow_id,
            "status": "COMPLETED",
            "reason_code": str(governor_result.get("reason_code")),
            "snapshot_id": snapshot["snapshot_id"],
            "decision": decision,
            "trigger_plan_patch": governor_result.get("trigger_plan_patch"),
            "applied_trigger_plan": governor_result.get("applied_trigger_plan"),
        }


def _failed(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "FAILED",
        "reason_code": reason_code,
        "snapshot_id": None,
        "decision": None,
        "trigger_plan_patch": None,
        "applied_trigger_plan": None,
    }
