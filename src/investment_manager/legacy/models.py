from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import (
    ExitReason,
    OrderType,
    ProgramExitCondition,
    Side,
)
from investment_manager.forecast.models import AssessmentOutcomeStatus, DirectionalView
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
    UnitInterval,
)


class Action(StrEnum):
    NO_ACTION = "NO_ACTION"
    OPEN = "OPEN"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    CANCEL_PENDING = "CANCEL_PENDING"


class CycleOutcome(StrEnum):
    NO_ACTION = "NO_ACTION"
    NO_TRADE = "NO_TRADE"
    RISK_REJECTED = "RISK_REJECTED"
    EXECUTION_PENDING = "EXECUTION_PENDING"
    EXECUTED = "EXECUTED"


class PriceCondition(FrozenModel):
    order_type: OrderType
    price: PositiveDecimal | None = None

    @field_validator("price")
    @classmethod
    def limit_requires_price(cls, value: Decimal | None, info):
        if info.data.get("order_type") == OrderType.LIMIT and value is None:
            raise ValueError("限价条件必须包含价格")
        return value


class SignalCandidate(FrozenModel):
    candidate_id: str
    cycle_id: str
    producer_id: str
    producer_version: str
    strategy_family: str
    symbol: str
    action: Action
    side: Side
    horizon_minutes: int = Field(gt=0)
    feature_refs: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
    entry: PriceCondition
    stop_price: PositiveDecimal
    valid_until: datetime
    signal_observed_at: datetime
    reference_price: PositiveDecimal
    expected_edge_half_life_seconds: int = Field(gt=0, le=86_400)
    raw_score: Decimal
    expected_gross_bps: Decimal
    calibration_ref: str
    program_exit: ProgramExitCondition | None = None
    execution_policy_version: str | None = None
    frequency_policy_version: str | None = None
    estimated_cost_bps: Decimal | None = Field(default=None, ge=0)
    unknowns: tuple[str, ...] = ()

    _utc_valid_until = field_validator("valid_until")(require_utc)
    _utc_signal_observed_at = field_validator("signal_observed_at")(require_utc)

    @model_validator(mode="after")
    def signal_timing_must_be_valid(self):
        if self.signal_observed_at >= self.valid_until:
            raise ValueError("SignalCandidate signal_observed_at 必须早于 valid_until")
        cost_basis = (
            self.execution_policy_version,
            self.frequency_policy_version,
            self.estimated_cost_bps,
        )
        if any(item is not None for item in cost_basis) and not all(
            item is not None for item in cost_basis
        ):
            raise ValueError("SignalCandidate 成本依据必须完整或全部缺失")
        return self

    @property
    def has_frozen_cost_basis(self) -> bool:
        return self.estimated_cost_bps is not None


class DirectionalForecast(FrozenModel):
    """与交易动作分离、可独立到期结算的一个方向预测。"""

    horizon_minutes: int = Field(gt=0)
    directional_view: DirectionalView
    confidence: UnitInterval


class AnalysisProposal(FrozenModel):
    """Codex 的受限 ACTION 输出；它不包含仓位、杠杆、风险金额或订单标识。"""

    proposal_id: str
    proposal_type: Literal["ACTION"] = "ACTION"
    suggested_action: Action
    symbol: str
    side: Side | None = None
    horizon_minutes: int | None = Field(default=None, gt=0)
    thesis: str = Field(min_length=1, max_length=2_000)
    evidence_ids: tuple[str, ...] = ()
    entry_condition: PriceCondition | None = None
    invalidation_price: PositiveDecimal | None = None
    valid_until: datetime | None = None
    confidence: UnitInterval
    unknowns: tuple[str, ...] = ()
    forecasts: tuple[DirectionalForecast, ...] = Field(min_length=1, max_length=4)

    _utc_valid_until = field_validator("valid_until")(optional_utc)

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_single_forecast(cls, value):
        """只在读取旧事实时收敛旧字段；新序列化结果不保留双重真相。"""

        if not isinstance(value, dict) or "forecasts" in value:
            return value
        if "directional_view" not in value and "view_horizon_minutes" not in value:
            return value
        upgraded = dict(value)
        directional_view = upgraded.pop("directional_view", DirectionalView.UNCERTAIN)
        horizon = upgraded.pop("view_horizon_minutes", 60)
        upgraded["forecasts"] = (
            {
                "horizon_minutes": horizon,
                "directional_view": directional_view,
                "confidence": upgraded.get("confidence", Decimal("0")),
            },
        )
        return upgraded

    @model_validator(mode="after")
    def open_requires_typed_trade_fields(self):
        required = (
            self.side,
            self.horizon_minutes,
            self.entry_condition,
            self.invalidation_price,
            self.valid_until,
        )
        if self.suggested_action == Action.OPEN and any(item is None for item in required):
            raise ValueError("OPEN proposal 必须包含方向、周期、入场、失效价格和有效期")
        if self.suggested_action == Action.NO_ACTION and any(item is not None for item in required):
            raise ValueError("NO_ACTION proposal 不得夹带交易参数")
        if self.suggested_action not in {Action.OPEN, Action.NO_ACTION}:
            raise ValueError("MVP PROPOSE 仅接受 OPEN 或 NO_ACTION")
        horizons = tuple(item.horizon_minutes for item in self.forecasts)
        if horizons != tuple(sorted(set(horizons))):
            raise ValueError("方向预测周期必须唯一且升序")
        return self

    def forecast_for_horizon(self, horizon_minutes: int) -> DirectionalForecast | None:
        return next(
            (
                forecast
                for forecast in self.forecasts
                if forecast.horizon_minutes == horizon_minutes
            ),
            None,
        )


class TradeIntent(FrozenModel):
    intent_id: str
    cycle_id: str
    pipeline_version: str
    composition_policy_version: str
    action: Action
    symbol: str
    side: Side
    candidate_ids: tuple[str, ...]
    entry: PriceCondition
    stop_price: PositiveDecimal
    max_holding_minutes: int = Field(gt=0)
    valid_until: datetime
    signal_observed_at: datetime
    reference_price: PositiveDecimal
    expected_edge_half_life_seconds: int = Field(gt=0, le=86_400)
    expected_gross_bps: Decimal
    program_exit: ProgramExitCondition | None = None

    _utc_valid_until = field_validator("valid_until")(require_utc)
    _utc_signal_observed_at = field_validator("signal_observed_at")(require_utc)

    @model_validator(mode="after")
    def signal_timing_must_be_valid(self):
        if self.signal_observed_at >= self.valid_until:
            raise ValueError("TradeIntent signal_observed_at 必须早于 valid_until")
        return self


class DecisionOutcome(FrozenModel):
    outcome_id: str
    cycle_id: str
    intent_id: str
    pipeline_version: str
    position_id: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    exit_reason: ExitReason
    quantity: PositiveDecimal
    entry_price: PositiveDecimal
    exit_price: PositiveDecimal
    gross_pnl: Decimal
    total_fees: Money
    net_pnl: Decimal
    maximum_favorable_excursion: Decimal
    maximum_adverse_excursion: Decimal

    _utc_opened_at = field_validator("opened_at")(require_utc)
    _utc_closed_at = field_validator("closed_at")(require_utc)


class CandidateOutcomeStatus(StrEnum):
    SETTLED = "SETTLED"
    UNSCORABLE = "UNSCORABLE"


class CandidateOutcome(FrozenModel):
    """Shadow 候选的到期反事实标签；不代表订单、持仓或账户收益。"""

    outcome_id: str
    candidate_id: str
    cycle_id: str
    producer_id: str
    producer_version: str
    calibration_ref: str
    evaluation_version: str
    execution_policy_version: str
    frequency_policy_version: str
    symbol: str
    side: Side
    status: CandidateOutcomeStatus
    signal_observed_at: datetime
    evaluation_at: datetime
    settled_at: datetime
    reference_price: PositiveDecimal
    entry_price: PositiveDecimal | None = None
    entry_event_time: datetime | None = None
    entry_observed_at: datetime | None = None
    exit_price: PositiveDecimal | None = None
    exit_event_time: datetime | None = None
    exit_observed_at: datetime | None = None
    gross_return_bps: Decimal | None = None
    estimated_cost_bps: Decimal = Field(ge=0)
    net_return_bps: Decimal | None = None
    reason_code: str

    _utc_signal_observed_at = field_validator("signal_observed_at")(require_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)
    _utc_settled_at = field_validator("settled_at")(require_utc)
    _utc_entry_event_time = field_validator("entry_event_time")(optional_utc)
    _utc_entry_observed_at = field_validator("entry_observed_at")(optional_utc)
    _utc_exit_event_time = field_validator("exit_event_time")(optional_utc)
    _utc_exit_observed_at = field_validator("exit_observed_at")(optional_utc)

    @model_validator(mode="after")
    def settlement_fields_must_match_status(self):
        outcome_values = (
            self.exit_price,
            self.exit_event_time,
            self.gross_return_bps,
            self.net_return_bps,
        )
        execution_values = (
            self.entry_price,
            self.entry_event_time,
            self.entry_observed_at,
            self.exit_observed_at,
        )
        if self.status == CandidateOutcomeStatus.SETTLED and any(
            item is None for item in outcome_values
        ):
            raise ValueError("SETTLED CandidateOutcome 必须包含完整收益事实")
        if self.status == CandidateOutcomeStatus.UNSCORABLE and any(
            item is not None for item in (*outcome_values, *execution_values)
        ):
            raise ValueError("UNSCORABLE CandidateOutcome 不得伪造执行或收益")
        if (
            self.evaluation_version == "outcome-window-v8"
            and self.status == (CandidateOutcomeStatus.SETTLED)
            and any(item is None for item in execution_values)
        ):
            raise ValueError("outcome-window-v8 必须包含完整入场与可见性事实")
        if any(item is None for item in execution_values) and any(
            item is not None for item in execution_values
        ):
            raise ValueError("CandidateOutcome 执行事实必须完整或全部缺失")
        if self.evaluation_at <= self.signal_observed_at:
            raise ValueError("CandidateOutcome 评价时间必须晚于信号时间")
        if self.settled_at < self.evaluation_at:
            raise ValueError("CandidateOutcome 不能在评价时间前结算")
        if self.entry_observed_at is not None and self.entry_observed_at < (
            self.signal_observed_at
        ):
            raise ValueError("CandidateOutcome 入场不能早于信号完成")
        return self


class AnalysisForecastOutcome(FrozenModel):
    """Non-tradable label for every Codex directional view, including abstention."""

    outcome_id: str
    proposal_id: str
    cycle_id: str
    pipeline_version: str
    analysis_behavior_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evaluation_version: str
    symbol: str
    directional_view: DirectionalView
    confidence: UnitInterval
    view_horizon_minutes: int = Field(gt=0)
    status: AssessmentOutcomeStatus
    signal_observed_at: datetime
    evaluation_at: datetime
    settled_at: datetime
    reference_price: PositiveDecimal
    exit_price: PositiveDecimal | None = None
    exit_event_time: datetime | None = None
    market_return_bps: Decimal | None = None
    directional_return_bps: Decimal | None = None
    direction_correct: bool | None = None
    reason_code: str

    _utc_signal_observed_at = field_validator("signal_observed_at")(require_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)
    _utc_settled_at = field_validator("settled_at")(require_utc)
    _utc_exit_event_time = field_validator("exit_event_time")(optional_utc)

    @model_validator(mode="after")
    def settlement_fields_match_status(self):
        market_facts = (self.exit_price, self.exit_event_time, self.market_return_bps)
        directional_facts = (self.directional_return_bps, self.direction_correct)
        if self.evaluation_at <= self.signal_observed_at:
            raise ValueError("方向预测评价时间必须晚于信号时间")
        if self.settled_at < self.evaluation_at:
            raise ValueError("方向预测不能在评价时间前结算")
        if self.status == AssessmentOutcomeStatus.UNSCORABLE:
            if any(item is not None for item in (*market_facts, *directional_facts)):
                raise ValueError("UNSCORABLE 方向预测不得伪造行情或方向结果")
            return self
        if any(item is None for item in market_facts):
            raise ValueError("可结算方向预测必须包含完整到期行情")
        if self.status == AssessmentOutcomeStatus.ABSTAINED:
            if self.directional_view != DirectionalView.UNCERTAIN or any(
                item is not None for item in directional_facts
            ):
                raise ValueError("ABSTAINED 只允许 UNCERTAIN 且不得伪造方向得分")
            return self
        if self.directional_view == DirectionalView.UNCERTAIN or any(
            item is None for item in directional_facts
        ):
            raise ValueError("SETTLED 方向预测必须包含 UP/DOWN 得分")
        return self
