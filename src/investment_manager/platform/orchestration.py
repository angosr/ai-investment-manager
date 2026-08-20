from __future__ import annotations

from typing import Protocol

from pydantic import Field, model_validator

from investment_manager.kernel.types import FrozenModel


class TemporalPolicyLike(Protocol):
    version: str
    activity_start_to_close_seconds: int
    activity_schedule_to_close_seconds: int
    retry_initial_seconds: int
    retry_maximum_seconds: int
    retry_backoff_coefficient: float
    retry_maximum_attempts: int


class OrchestrationPolicySnapshot(FrozenModel):
    """随 Workflow 输入冻结，避免运行中配置漂移改变重放结果。"""

    version: str
    activity_start_to_close_seconds: int = Field(ge=10, le=900)
    activity_schedule_to_close_seconds: int = Field(ge=10, le=1800)
    retry_initial_seconds: int = Field(ge=1, le=60)
    retry_maximum_seconds: int = Field(ge=1, le=300)
    retry_backoff_coefficient: float = Field(ge=1, le=10)
    retry_maximum_attempts: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def bounds_must_be_consistent(self):
        if self.activity_schedule_to_close_seconds < self.activity_start_to_close_seconds:
            raise ValueError("schedule-to-close 不得短于 start-to-close")
        if self.retry_maximum_seconds < self.retry_initial_seconds:
            raise ValueError("最大重试间隔不得短于初始间隔")
        return self

    @classmethod
    def from_config(cls, policy: TemporalPolicyLike) -> OrchestrationPolicySnapshot:
        return cls(
            version=policy.version,
            activity_start_to_close_seconds=policy.activity_start_to_close_seconds,
            activity_schedule_to_close_seconds=policy.activity_schedule_to_close_seconds,
            retry_initial_seconds=policy.retry_initial_seconds,
            retry_maximum_seconds=policy.retry_maximum_seconds,
            retry_backoff_coefficient=policy.retry_backoff_coefficient,
            retry_maximum_attempts=policy.retry_maximum_attempts,
        )
