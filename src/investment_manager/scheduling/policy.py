from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.kernel.configuration import StrictConfig


class TriggerPolicy(StrictConfig):
    version: str
    heartbeat_minutes: int = Field(default=15, ge=1, le=240)
    debounce_seconds: int = Field(default=120, ge=0, le=3600)
    minimum_intelligence_review_priority: Decimal = Field(
        default=Decimal("0.80"), ge=0, le=1
    )
    volatility_jump_threshold: Decimal = Field(default=Decimal("0.01"), gt=0)
    volatility_window_seconds: int = Field(default=600, ge=60, le=86_400)
    minimum_call_interval_seconds: int = Field(default=15, ge=1, le=3600)
    maximum_scheduled_wakeups: int = Field(default=64, ge=1, le=500)
    maximum_event_rules: int = Field(default=32, ge=1, le=200)
    maximum_batch_size: int = Field(default=100, ge=1, le=1000)
    maximum_pending_triggers: int = Field(default=1000, ge=1, le=10000)
    trigger_expiry_seconds: int = Field(default=900, ge=30, le=86400)
    outbox_fallback_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    dispatcher_advisory_lock_key: int = Field(default=817_221_901, ge=1)


class TemporalPolicy(StrictConfig):
    """持久化编排参数；每次启动 Workflow 时会冻结为输入的一部分。"""

    version: str
    address: str = "127.0.0.1:7233"
    namespace: str = "default"
    task_queue: str = "investment-manager-analysis-v1"
    assessment_task_queue: str = "investment-manager-assessment-v1"
    trigger_task_queue: str = "investment-manager-trigger-v1"
    activity_start_to_close_seconds: int = Field(default=240, ge=10, le=900)
    activity_schedule_to_close_seconds: int = Field(default=600, ge=10, le=1800)
    retry_initial_seconds: int = Field(default=2, ge=1, le=60)
    retry_maximum_seconds: int = Field(default=30, ge=1, le=300)
    retry_backoff_coefficient: float = Field(default=2.0, ge=1, le=10)
    retry_maximum_attempts: int = Field(default=3, ge=1, le=10)
    worker_threads: int = Field(default=4, ge=1, le=32)

    @model_validator(mode="after")
    def temporal_bounds_must_be_consistent(self):
        if self.activity_schedule_to_close_seconds < self.activity_start_to_close_seconds:
            raise ValueError("Temporal schedule-to-close 不得短于 start-to-close")
        if self.retry_maximum_seconds < self.retry_initial_seconds:
            raise ValueError("Temporal 最大重试间隔不得短于初始间隔")
        if not all(
            item.strip()
            for item in (
                self.address,
                self.namespace,
                self.task_queue,
                self.assessment_task_queue,
                self.trigger_task_queue,
            )
        ):
            raise ValueError("Temporal address、namespace、task_queue 不得为空")
        return self
