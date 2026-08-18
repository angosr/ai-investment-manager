from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

LIFECYCLE_ACTIVITY_NAME = "evaluate-position-lifecycle-v1"


@workflow.defn(name="PositionLifecycleWorkflow")
class PositionLifecycleWorkflow:
    """持久化持仓监控进度；价格读取和退出动作全部位于 Activity。"""

    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        raw_hash = request.get("input_hash")
        workflow_id = str(request.get("workflow_id") or workflow.info().workflow_id)
        if not isinstance(raw_hash, str) or not raw_hash:
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")
        self._input_hash = raw_hash
        try:
            initial_lifecycle = request["initial_lifecycle"]
            current_lifecycle = request["lifecycle"]
            if (
                not isinstance(initial_lifecycle, dict)
                or not isinstance(current_lifecycle, dict)
                or initial_lifecycle.get("position_id") != current_lifecycle.get("position_id")
            ):
                raise ValueError
            poll_seconds = int(request["poll_seconds"])
            orchestration = request["orchestration"]
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=int(orchestration["retry_initial_seconds"])),
                maximum_interval=timedelta(seconds=int(orchestration["retry_maximum_seconds"])),
                backoff_coefficient=float(orchestration["retry_backoff_coefficient"]),
                maximum_attempts=int(orchestration["retry_maximum_attempts"]),
                non_retryable_error_types=["InvalidLifecycleInput"],
            )
            start_to_close = timedelta(
                seconds=int(orchestration["activity_start_to_close_seconds"])
            )
            schedule_to_close = timedelta(
                seconds=int(orchestration["activity_schedule_to_close_seconds"])
            )
            if poll_seconds < 1:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")

        while True:
            try:
                result = await workflow.execute_activity(
                    LIFECYCLE_ACTIVITY_NAME,
                    request,
                    result_type=dict,
                    start_to_close_timeout=start_to_close,
                    schedule_to_close_timeout=schedule_to_close,
                    retry_policy=retry_policy,
                    summary="评估持仓保护与退出条件",
                )
            except ActivityError:
                await workflow.sleep(timedelta(seconds=poll_seconds))
                continue
            if not isinstance(result, dict) or not isinstance(result.get("lifecycle"), dict):
                return _failed(workflow_id, "INVALID_ACTIVITY_RESULT")
            request["lifecycle"] = result["lifecycle"]
            if result.get("closed") is True:
                return {
                    "workflow_id": workflow_id,
                    "status": "CLOSED",
                    "reason_code": str(result.get("reason_code") or "POSITION_CLOSED"),
                    "lifecycle": result["lifecycle"],
                }
            await workflow.sleep(timedelta(seconds=poll_seconds))


def _failed(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "FAILED",
        "reason_code": reason_code,
        "lifecycle": None,
    }
