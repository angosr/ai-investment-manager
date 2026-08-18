from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

OUTCOME_EVALUATION_ACTIVITY_NAME = "evaluate-outcome-window-v1"


@workflow.defn(name="OutcomeEvaluationWorkflow")
class OutcomeEvaluationWorkflow:
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
            poll_seconds = int(request["poll_seconds"])
            orchestration = request["orchestration"]
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=int(orchestration["retry_initial_seconds"])),
                maximum_interval=timedelta(seconds=int(orchestration["retry_maximum_seconds"])),
                backoff_coefficient=float(orchestration["retry_backoff_coefficient"]),
                maximum_attempts=int(orchestration["retry_maximum_attempts"]),
                non_retryable_error_types=["InvalidOutcomeEvaluationInput"],
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
                    OUTCOME_EVALUATION_ACTIVITY_NAME,
                    request,
                    result_type=dict,
                    start_to_close_timeout=start_to_close,
                    schedule_to_close_timeout=schedule_to_close,
                    retry_policy=retry_policy,
                    summary="聚合固定窗口的已结算逐笔结果",
                )
            except ActivityError:
                await workflow.sleep(timedelta(seconds=poll_seconds))
                continue
            report = result.get("report") if isinstance(result, dict) else None
            attempt = result.get("attempt") if isinstance(result, dict) else None
            if not isinstance(report, dict) or not isinstance(attempt, int):
                return _failed(workflow_id, "INVALID_ACTIVITY_RESULT")
            if report.get("status") == "COMPLETE":
                return {
                    "workflow_id": workflow_id,
                    "status": "COMPLETED",
                    "reason_code": "OUTCOME_WINDOW_COMPLETE",
                    "attempt": attempt,
                    "report": report,
                }
            await workflow.sleep(timedelta(seconds=poll_seconds))


def _failed(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "FAILED",
        "reason_code": reason_code,
        "attempt": 0,
        "report": None,
    }
