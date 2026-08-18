from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[Decimal, Field(ge=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
UnitInterval = Annotated[Decimal, Field(ge=0, le=1)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _require_utc(value) if value is not None else None


class Action(StrEnum):
    NO_ACTION = "NO_ACTION"
    OPEN = "OPEN"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    CANCEL_PENDING = "CANCEL_PENDING"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class GuardState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class RiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class CycleOutcome(StrEnum):
    NO_ACTION = "NO_ACTION"
    NO_TRADE = "NO_TRADE"
    RISK_REJECTED = "RISK_REJECTED"
    EXECUTION_PENDING = "EXECUTION_PENDING"
    EXECUTED = "EXECUTED"


class OrderStatus(StrEnum):
    RISK_ACCEPTED = "RISK_ACCEPTED"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class PositionLifecycleStatus(StrEnum):
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED = "PROTECTED"
    PROTECTION_FAILED = "PROTECTION_FAILED"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    MAX_HOLDING_TIME = "MAX_HOLDING_TIME"
    PROTECTION_FAILURE = "PROTECTION_FAILURE"


class MarketBar(FrozenModel):
    event_time: datetime
    observed_at: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: Money

    _utc_event_time = field_validator("event_time")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)

    @field_validator("high")
    @classmethod
    def high_must_cover_prices(cls, value: Decimal, info):
        values = info.data
        if "open" in values and value < values["open"]:
            raise ValueError("high 不能低于 open")
        return value

    @field_validator("close")
    @classmethod
    def close_must_be_in_range(cls, value: Decimal, info):
        low = info.data.get("low")
        high = info.data.get("high")
        if low is not None and high is not None and not low <= value <= high:
            raise ValueError("close 必须位于 low 与 high 之间")
        return value


class MarketSnapshot(FrozenModel):
    cycle_id: str
    symbol: str
    as_of: datetime
    observed_at: datetime
    bid: PositiveDecimal
    ask: PositiveDecimal
    last: PositiveDecimal
    bars: tuple[MarketBar, ...] = Field(min_length=2)
    source: str

    _utc_as_of = field_validator("as_of")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)

    @field_validator("observed_at")
    @classmethod
    def observation_must_be_visible(cls, value: datetime, info):
        as_of = info.data.get("as_of")
        if as_of is not None and value > as_of:
            raise ValueError("行情 observed_at 不能晚于 as_of")
        return value

    @field_validator("ask")
    @classmethod
    def ask_not_below_bid(cls, value: Decimal, info):
        bid = info.data.get("bid")
        if bid is not None and value < bid:
            raise ValueError("ask 不能低于 bid")
        return value

    @field_validator("bars")
    @classmethod
    def bars_must_be_visible_and_sorted(
        cls, bars: tuple[MarketBar, ...], info
    ) -> tuple[MarketBar, ...]:
        as_of = info.data.get("as_of")
        if any(left.event_time >= right.event_time for left, right in pairwise(bars)):
            raise ValueError("bars 必须按 event_time 严格递增")
        if as_of is not None and any(
            bar.observed_at > as_of or bar.event_time > as_of for bar in bars
        ):
            raise ValueError("快照不能包含 as_of 之后才观察到的行情")
        return bars


class Position(FrozenModel):
    symbol: str
    quantity: Decimal
    average_price: Money


class AccountSnapshot(FrozenModel):
    cycle_id: str
    as_of: datetime
    observed_at: datetime
    quote_balance: Money
    positions: tuple[Position, ...] = ()
    open_order_count: int = Field(default=0, ge=0)
    daily_pnl: Decimal = Decimal("0")
    drawdown_fraction: UnitInterval = Decimal("0")
    reconciled: bool = True

    _utc_as_of = field_validator("as_of")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)


class IntelligenceEvent(FrozenModel):
    evidence_id: str
    event_time: datetime
    observed_at: datetime
    source: str
    title: str
    body: str
    symbols: tuple[str, ...]
    relevance: UnitInterval
    impact: UnitInterval
    source_reliability: UnitInterval
    novelty: UnitInterval

    _utc_event_time = field_validator("event_time")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)


class FeatureSnapshot(FrozenModel):
    cycle_id: str
    symbol: str
    as_of: datetime
    feature_set_version: str
    return_fraction: Decimal
    realized_volatility: Money
    atr: Money
    spread_bps: Money
    volume_ratio: Money
    regime: Literal["TRENDING_UP", "TRENDING_DOWN", "RANGING", "UNKNOWN"]
    market_age_seconds: int = Field(ge=0)

    _utc_as_of = field_validator("as_of")(_require_utc)


class PanelEvidence(FrozenModel):
    evidence_id: str
    event_time: datetime
    observed_at: datetime
    source: str
    title: str
    excerpt: str
    value_score: Decimal
    prompt_injection_suspected: bool = False

    _utc_event_time = field_validator("event_time")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)


class PanelSnapshot(FrozenModel):
    cycle_id: str
    as_of: datetime
    schema_version: str
    policy_version: str
    symbol: str
    account: AccountSnapshot
    market: MarketSnapshot
    features: FeatureSnapshot
    evidence: tuple[PanelEvidence, ...]
    data_quality: tuple[str, ...]
    rules_digest: tuple[str, ...]
    content_hash: str

    _utc_as_of = field_validator("as_of")(_require_utc)


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
    unknowns: tuple[str, ...] = ()

    _utc_valid_until = field_validator("valid_until")(_require_utc)
    _utc_signal_observed_at = field_validator("signal_observed_at")(_require_utc)

    @model_validator(mode="after")
    def signal_timing_must_be_valid(self):
        if self.signal_observed_at >= self.valid_until:
            raise ValueError("SignalCandidate signal_observed_at 必须早于 valid_until")
        return self


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

    _utc_valid_until = field_validator("valid_until")(_optional_utc)

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
        return self


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

    _utc_valid_until = field_validator("valid_until")(_require_utc)
    _utc_signal_observed_at = field_validator("signal_observed_at")(_require_utc)

    @model_validator(mode="after")
    def signal_timing_must_be_valid(self):
        if self.signal_observed_at >= self.valid_until:
            raise ValueError("TradeIntent signal_observed_at 必须早于 valid_until")
        return self


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

    _utc_expires_at = field_validator("expires_at")(_require_utc)


class RiskDecision(FrozenModel):
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


class Fill(FrozenModel):
    fill_id: str
    order_id: str
    event_time: datetime
    price: PositiveDecimal
    quantity: PositiveDecimal
    fee: Money

    _utc_event_time = field_validator("event_time")(_require_utc)


class Order(FrozenModel):
    order_id: str
    client_order_id: str
    cycle_id: str
    intent_id: str
    symbol: str
    side: Side
    order_type: OrderType
    requested_quantity: PositiveDecimal
    limit_price: PositiveDecimal | None = None
    status: OrderStatus
    fills: tuple[Fill, ...] = ()


class PositionLifecycle(FrozenModel):
    position_id: str
    cycle_id: str
    intent_id: str
    entry_order_id: str
    reservation_id: str
    symbol: str
    quantity: PositiveDecimal
    entry_price: PositiveDecimal
    entry_fee: Money
    stop_price: PositiveDecimal
    opened_at: datetime
    max_exit_at: datetime
    highest_price: PositiveDecimal
    lowest_price: PositiveDecimal
    status: PositionLifecycleStatus
    protection_id: str | None = None
    closed_at: datetime | None = None
    exit_order_id: str | None = None
    exit_reason: ExitReason | None = None

    _utc_opened_at = field_validator("opened_at")(_require_utc)
    _utc_max_exit_at = field_validator("max_exit_at")(_require_utc)
    _utc_closed_at = field_validator("closed_at")(_optional_utc)


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

    _utc_opened_at = field_validator("opened_at")(_require_utc)
    _utc_closed_at = field_validator("closed_at")(_require_utc)


class MetricObservation(FrozenModel):
    metric_id: str
    metric_version: str
    cycle_id: str
    observed_at: datetime
    value: Decimal
    dimensions: tuple[tuple[str, str], ...] = ()

    _utc_observed_at = field_validator("observed_at")(_require_utc)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
