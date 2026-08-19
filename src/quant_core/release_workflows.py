from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from quant_core.temporal_compat import default_activity_versioning_intent

EVALUATE_RELEASE_ACTIVITY = "evaluate-release-approval-v1"


@workflow.defn(name="ReleaseWorkflow")
class ReleaseWorkflow:
    """只生成审批事实，不包含发布、切流或修改 Champion 的能力。"""

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
                non_retryable_error_types=["InvalidReleaseInput"],
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
            raw_result = await workflow.execute_activity(
                EVALUATE_RELEASE_ACTIVITY,
                request,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=schedule_to_close,
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="核验候选版本并生成不可执行的人工审批请求",
            )
        except ActivityError:
            return _failed(workflow_id, "RELEASE_GATE_ACTIVITY_FAILED")
        decision = raw_result.get("decision")
        if not isinstance(decision, dict):
            return _failed(workflow_id, "INVALID_RELEASE_GATE_RESULT")
        return {
            "workflow_id": workflow_id,
            "status": "COMPLETED",
            "reason_code": str(raw_result.get("reason_code")),
            "decision": decision,
        }


def _failed(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "FAILED",
        "reason_code": reason_code,
        "decision": None,
    }
