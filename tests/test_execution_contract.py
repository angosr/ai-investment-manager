from __future__ import annotations

import pytest

from quant_core.cycle import AnalysisCycle
from quant_core.domain import CycleOutcome
from quant_core.execution_contract import (
    ExecutionRequest,
    RiskTransition,
    build_execution_request,
    build_execution_result,
)


def test_execution_request_has_stable_identity_and_hash(app_config, replay_input) -> None:
    result = AnalysisCycle.create(app_config).run(replay_input)
    assert result.intent is not None
    assert result.risk_decision is not None
    request = build_execution_request(
        intent=result.intent,
        risk_decision=result.risk_decision,
        market=replay_input.market,
        account=replay_input.account,
        execution_policy_version=app_config.execution.version,
    )
    replayed = build_execution_request(
        intent=result.intent,
        risk_decision=result.risk_decision,
        market=replay_input.market,
        account=replay_input.account,
        execution_policy_version=app_config.execution.version,
    )

    assert replayed == request
    raw = request.model_dump(mode="json")
    raw["execution_policy_version"] = "tampered"
    with pytest.raises(ValueError, match=r"execution_id|request_hash"):
        ExecutionRequest.model_validate(raw)


def test_execution_result_is_hashed_and_requires_account_for_fills(
    app_config, replay_input
) -> None:
    cycle_result = AnalysisCycle.create(app_config).run(replay_input)
    assert cycle_result.intent is not None
    assert cycle_result.risk_decision is not None
    assert cycle_result.order is not None
    request = build_execution_request(
        intent=cycle_result.intent,
        risk_decision=cycle_result.risk_decision,
        market=replay_input.market,
        account=replay_input.account,
        execution_policy_version=app_config.execution.version,
    )
    result = build_execution_result(
        execution_id=request.execution_id,
        cycle_id=request.cycle_id,
        outcome=CycleOutcome.EXECUTED,
        reason_code=cycle_result.reason_code,
        risk_transition=RiskTransition.CONSUMED,
        order=cycle_result.order,
        account_after=cycle_result.account_after,
        position_lifecycle=cycle_result.position_lifecycle,
        exit_order=cycle_result.exit_order,
        decision_outcome=cycle_result.decision_outcome,
        metrics=cycle_result.metrics,
        outcome_metrics=cycle_result.outcome_metrics,
    )
    assert result.result_hash

    invalid = result.model_dump(mode="python")
    invalid["account_after"] = None
    invalid["result_hash"] = "invalid"
    with pytest.raises(ValueError, match="账户快照"):
        type(result).model_validate(invalid)
