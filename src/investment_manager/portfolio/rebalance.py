"""Authoritative cadence fact for one Portfolio rebalance period."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.configuration import StrictConfig
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel


class PortfolioRebalancePolicy(StrictConfig):
    """One monthly decision near the first UTC open; no late catch-up."""

    version: str = Field(min_length=1)
    maximum_entry_delay_minutes: int = Field(default=30, ge=1, le=1_440)


class RebalancePeriodMode(StrEnum):
    DECIDE = "DECIDE"
    NO_CHANGE = "NO_CHANGE"


class PortfolioRebalancePeriod(FrozenModel):
    """First-writer-wins fact freezing one decision opportunity per calendar month."""

    period_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    period_start: datetime
    period_end: datetime
    entry_window_end: datetime
    decision_at: datetime
    mode: RebalancePeriodMode
    candidate_forecast_id: str | None = None
    reason_codes: tuple[str, ...] = Field(min_length=1)

    _utc_period_start = field_validator("period_start")(require_utc)
    _utc_period_end = field_validator("period_end")(require_utc)
    _utc_entry_window_end = field_validator("entry_window_end")(require_utc)
    _utc_decision_at = field_validator("decision_at")(require_utc)

    @classmethod
    def create(
        cls,
        *,
        portfolio_id: str,
        policy_version: str,
        period_start: datetime,
        period_end: datetime,
        entry_window_end: datetime,
        decision_at: datetime,
        candidate_forecast_id: str | None,
    ) -> PortfolioRebalancePeriod:
        mode = (
            RebalancePeriodMode.DECIDE
            if candidate_forecast_id is not None
            else RebalancePeriodMode.NO_CHANGE
        )
        return cls(
            period_id=stable_id(
                "portfolio_rebalance_period",
                portfolio_id,
                policy_version,
                require_utc(period_start).isoformat(),
            ),
            portfolio_id=portfolio_id,
            policy_version=policy_version,
            period_start=period_start,
            period_end=period_end,
            entry_window_end=entry_window_end,
            decision_at=decision_at,
            mode=mode,
            candidate_forecast_id=candidate_forecast_id,
            reason_codes=(
                ("MONTHLY_REBALANCE_DECISION_FROZEN",)
                if mode == RebalancePeriodMode.DECIDE
                else ("MONTHLY_ENTRY_WINDOW_MISSED_NO_CHANGE",)
            ),
        )

    @property
    def cycle_id(self) -> str:
        return stable_id("capital_cycle", self.period_id, self.decision_at.isoformat())

    @model_validator(mode="after")
    def identity_window_and_mode_are_consistent(self):
        if not (
            self.period_start
            < self.entry_window_end
            <= self.period_end
            and self.period_start <= self.decision_at < self.period_end
        ):
            raise ValueError("Portfolio 再平衡周期或决策时点非法")
        expected_id = stable_id(
            "portfolio_rebalance_period",
            self.portfolio_id,
            self.policy_version,
            self.period_start.isoformat(),
        )
        if self.period_id != expected_id:
            raise ValueError("Portfolio 再平衡周期 identity 不一致")
        if (self.mode == RebalancePeriodMode.DECIDE) != (
            self.candidate_forecast_id is not None
        ):
            raise ValueError("Portfolio 再平衡模式与候选 Forecast 不一致")
        if self.mode == RebalancePeriodMode.DECIDE and (
            self.decision_at >= self.entry_window_end
        ):
            raise ValueError("Portfolio 不允许在月度窗口后冻结新风险决策")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("Portfolio 再平衡 reason_codes 必须唯一且排序")
        return self
