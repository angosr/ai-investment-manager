from __future__ import annotations

from enum import StrEnum

from investment_manager.kernel.types import FrozenModel


class GuardState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class RiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RuleResult(FrozenModel):
    rule_id: str
    rule_version: str
    state: GuardState
    reason_code: str
    observed: str | None = None
    limit: str | None = None
