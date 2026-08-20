from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError

from investment_manager.platform.temporal import default_activity_versioning_intent

PREPARE_ACTIVITY_NAME = "prepare-analysis-decision-v1"
EXECUTION_ACTIVITY_NAME = "execute-trade-request-v1"
ANALYSIS_CYCLE_WORKFLOW_NAME = "AnalysisCycleWorkflow"


def _activity_options(request: dict[str, Any]) -> tuple[datetime, timedelta, int, RetryPolicy]:
    deadline = datetime.fromisoformat(str(request["deadline"]))
    policy = request["orchestration"]
    start_to_close = timedelta(seconds=int(policy["activity_start_to_close_seconds"]))
    schedule_seconds = int(policy["activity_schedule_to_close_seconds"])
    retry_policy = RetryPolicy(
        initial_interval=timedelta(seconds=int(policy["retry_initial_seconds"])),
        maximum_interval=timedelta(seconds=int(policy["retry_maximum_seconds"])),
        backoff_coefficient=float(policy["retry_backoff_coefficient"]),
        maximum_attempts=int(policy["retry_maximum_attempts"]),
        non_retryable_error_types=["InvalidWorkflowInput", "PermanentDomainError"],
    )
    return deadline, start_to_close, schedule_seconds, retry_policy


@workflow.defn(name="ExecutionWorkflow")
class ExecutionWorkflow:
    """只消费一个不可变 ExecutionRequest；稳定 ID 防止重复执行。"""

    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        execution_request = request.get("execution_request")
        if not isinstance(execution_request, dict):
            raise ValueError("ExecutionWorkflow 缺少 execution_request")
        raw_hash = execution_request.get("request_hash")
        if not isinstance(raw_hash, str) or not raw_hash:
            raise ValueError("ExecutionWorkflow 缺少 request_hash")
        self._input_hash = raw_hash
        deadline, start_to_close, configured_schedule, retry_policy = _activity_options(request)
        remaining = deadline - workflow.now()
        activity_result: dict[str, Any]
        if remaining > timedelta(0):
            try:
                activity_result = await workflow.execute_activity(
                    EXECUTION_ACTIVITY_NAME,
                    execution_request,
                    result_type=dict,
                    start_to_close_timeout=start_to_close,
                    schedule_to_close_timeout=min(
                        timedelta(seconds=configured_schedule),
                        remaining,
                    ),
                    retry_policy=retry_policy,
                    versioning_intent=default_activity_versioning_intent(),
                    summary="幂等执行冻结交易请求",
                )
            except ActivityError:
                activity_result = await self._recover_terminal_state(
                    execution_request,
                    start_to_close=start_to_close,
                    retry_policy=retry_policy,
                )
        else:
            activity_result = await self._recover_terminal_state(
                execution_request,
                start_to_close=start_to_close,
                retry_policy=retry_policy,
            )
        cycle_result = activity_result.get("cycle_result")
        attempt = activity_result.get("attempt")
        if not isinstance(cycle_result, dict) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("Execution Activity 返回无效")
        return {"attempt": attempt, "cycle_result": cycle_result}

    @staticmethod
    async def _recover_terminal_state(
        execution_request: dict[str, Any],
        *,
        start_to_close: timedelta,
        retry_policy: RetryPolicy,
    ) -> dict[str, Any]:
        """只在执行所有权仍属于本 Workflow 时恢复；无法确认就持续重试。"""

        recovery_retry = RetryPolicy(
            initial_interval=retry_policy.initial_interval,
            maximum_interval=retry_policy.maximum_interval,
            backoff_coefficient=retry_policy.backoff_coefficient,
            maximum_attempts=0,
            non_retryable_error_types=retry_policy.non_retryable_error_types,
        )
        return await workflow.execute_activity(
            EXECUTION_ACTIVITY_NAME,
            execution_request,
            result_type=dict,
            start_to_close_timeout=start_to_close,
            retry_policy=recovery_retry,
            versioning_intent=default_activity_versioning_intent(),
            summary="确认订单终态并回收风险预留",
        )


@workflow.defn(name=ANALYSIS_CYCLE_WORKFLOW_NAME)
class AnalysisCycleWorkflow:
    """冻结决策后，用稳定 ID 子 Workflow 完成执行交接。"""

    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(request.get("workflow_id") or workflow.info().workflow_id)
        raw_input_hash = request.get("input_hash")
        if not isinstance(raw_input_hash, str) or not raw_input_hash:
            return _no_trade(workflow_id, "INVALID_WORKFLOW_INPUT")
        self._input_hash = raw_input_hash
        try:
            deadline, start_to_close, configured_schedule, retry_policy = _activity_options(request)
        except (KeyError, TypeError, ValueError):
            return _no_trade(workflow_id, "INVALID_WORKFLOW_INPUT")

        remaining = deadline - workflow.now()
        if remaining <= timedelta(0):
            return _no_trade(workflow_id, "ANALYSIS_DEADLINE_EXPIRED")
        try:
            activity_result = await workflow.execute_activity(
                PREPARE_ACTIVITY_NAME,
                request,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=min(
                    timedelta(seconds=configured_schedule),
                    remaining,
                ),
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="冻结分析决策与执行请求",
            )
        except ActivityError:
            return _no_trade(workflow_id, "ANALYSIS_ACTIVITY_FAILED")

        if not isinstance(activity_result, dict):
            return _no_trade(workflow_id, "INVALID_ACTIVITY_RESULT")
        cycle_result = activity_result.get("cycle_result")
        analysis_attempt = activity_result.get("attempt")
        if not isinstance(cycle_result, dict) or not isinstance(analysis_attempt, int):
            return _no_trade(workflow_id, "INVALID_ACTIVITY_RESULT")

        if cycle_result.get("outcome") == "EXECUTION_PENDING":
            execution_request = cycle_result.get("execution_request")
            if not isinstance(execution_request, dict):
                return _no_trade(workflow_id, "INVALID_EXECUTION_HANDOFF")
            child_input = {
                "execution_request": execution_request,
                "orchestration": request["orchestration"],
                "deadline": request["deadline"],
            }
            child_result = await workflow.execute_child_workflow(
                ExecutionWorkflow.run,
                child_input,
                id=str(execution_request["execution_id"]),
                result_type=dict,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                static_summary="执行冻结的交易请求",
            )
            cycle_result = child_result["cycle_result"]
            execution_attempt = int(child_result["attempt"])
        else:
            execution_attempt = 0

        return {
            "workflow_id": workflow_id,
            "status": "COMPLETED",
            "reason_code": str(cycle_result.get("reason_code", "UNKNOWN")),
            "attempt": max(analysis_attempt, execution_attempt),
            "cycle_result": cycle_result,
        }


def _no_trade(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "NO_TRADE",
        "reason_code": reason_code,
        "attempt": 0,
        "cycle_result": None,
    }
