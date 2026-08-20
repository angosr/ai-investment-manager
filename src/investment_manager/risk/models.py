from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import field_validator

from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal


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


class RiskReservation(FrozenModel):
    reservation_id: str
    cycle_id: str
    intent_id: str
    symbol: str
    risk_amount: Money
    quantity: PositiveDecimal
    expires_at: datetime

    _utc_expires_at = field_validator("expires_at")(require_utc)


class RiskDecision(FrozenModel):
    """Legacy single-intent authorization retained until the old chain is removed."""

    decision_id: str
    cycle_id: str
    intent_id: str
    outcome: RiskOutcome
    policy_version: str
    intent_hash: str
    account_snapshot_hash: str
    market_snapshot_hash: str
    rule_results: tuple[RuleResult, ...]
    quantity: PositiveDecimal | None = None
    reservation: RiskReservation | None = None
