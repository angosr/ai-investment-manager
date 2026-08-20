from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.asset_management import (
    AssessmentUncertainty,
    ContextAssessment,
    ContextView,
    PricedState,
)
from investment_manager.candidate_evaluation import trade_at_or_before
from investment_manager.decision_packet import DecisionPacket
from investment_manager.domain import (
    DirectionalView,
    ForecastOutcomeStatus,
    FrozenModel,
    PositiveDecimal,
    _optional_utc,
    _require_utc,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.persistence import (
    assessment_view_outcomes,
    context_assessments,
    decision_packets,
)


class AssessmentViewOutcome(FrozenModel):
    outcome_id: str = Field(min_length=1)
    assessment_id: str = Field(min_length=1)
    decision_packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_scope: str = Field(min_length=1)
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_version: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    already_priced: PricedState
    uncertainty: AssessmentUncertainty
    status: ForecastOutcomeStatus
    signal_observed_at: datetime
    evaluation_at: datetime
    settled_at: datetime
    reference_price: PositiveDecimal | None = None
    exit_price: PositiveDecimal | None = None
    exit_event_time: datetime | None = None
    market_return_bps: Decimal | None = None
    directional_return_bps: Decimal | None = None
    direction_correct: bool | None = None
    reason_code: str = Field(min_length=1)

    _utc_signal_observed_at = field_validator("signal_observed_at")(_require_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(_require_utc)
    _utc_settled_at = field_validator("settled_at")(_require_utc)
    _utc_exit_event_time = field_validator("exit_event_time")(_optional_utc)

    @model_validator(mode="after")
    def identity_and_settlement_must_match(self):
        expected_id = stable_id(
            "assessment_view_outcome",
            self.assessment_id,
            self.asset,
            self.horizon_minutes,
            self.evaluation_version,
        )
        if self.outcome_id != expected_id:
            raise ValueError("AssessmentViewOutcome outcome_id 不匹配")
        if self.evaluation_at != self.signal_observed_at + timedelta(
            minutes=self.horizon_minutes
        ):
            raise ValueError("AssessmentViewOutcome 评价时间与预测周期不一致")
        if self.settled_at < self.evaluation_at:
            raise ValueError("AssessmentViewOutcome 不能提前结算")
        market_facts = (self.exit_price, self.exit_event_time, self.market_return_bps)
        directional_facts = (self.directional_return_bps, self.direction_correct)
        if self.status == ForecastOutcomeStatus.UNSCORABLE:
            if any(item is not None for item in (*market_facts, *directional_facts)):
                raise ValueError("UNSCORABLE Assessment view 不得伪造行情结果")
            return self
        if self.reference_price is None or any(item is None for item in market_facts):
            raise ValueError("可结算 Assessment view 必须包含完整到期行情")
        if self.status == ForecastOutcomeStatus.ABSTAINED:
            if self.direction != DirectionalView.UNCERTAIN or any(
                item is not None for item in directional_facts
            ):
                raise ValueError("ABSTAINED 只允许 UNCERTAIN 且不得伪造方向收益")
            return self
        if self.direction == DirectionalView.UNCERTAIN or any(
            item is None for item in directional_facts
        ):
            raise ValueError("SETTLED Assessment view 必须包含 UP/DOWN 方向收益")
        return self


@dataclass(frozen=True, slots=True)
class PendingAssessmentView:
    assessment: ContextAssessment
    packet: DecisionPacket
    view: ContextView
    symbol: str


@dataclass(frozen=True, slots=True)
class AssessmentSettlementResult:
    settled: int = 0
    abstained: int = 0
    unscorable: int = 0
    pending: int = 0


class SqlAssessmentViewOutcomeStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def pending(
        self,
        *,
        evaluation_version: str,
        limit: int = 100,
    ) -> tuple[PendingAssessmentView, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Assessment view 结算批次必须在 1..1000")
        if not evaluation_version:
            raise ValueError("evaluation_version 不能为空")
        with self._engine.connect() as connection:
            outcome_counts = (
                select(
                    assessment_view_outcomes.c.assessment_id,
                    func.count(assessment_view_outcomes.c.outcome_id).label(
                        "outcome_count"
                    ),
                )
                .where(
                    assessment_view_outcomes.c.evaluation_version
                    == evaluation_version
                )
                .group_by(assessment_view_outcomes.c.assessment_id)
                .subquery()
            )
            rows = tuple(
                connection.execute(
                    select(
                        context_assessments.c.payload.label("assessment"),
                        decision_packets.c.payload.label("packet"),
                    )
                    .join(
                        decision_packets,
                        decision_packets.c.packet_id == context_assessments.c.packet_id,
                    )
                    .outerjoin(
                        outcome_counts,
                        outcome_counts.c.assessment_id
                        == context_assessments.c.assessment_id,
                    )
                    .where(
                        func.coalesce(outcome_counts.c.outcome_count, 0)
                        < context_assessments.c.view_count
                    )
                    .order_by(
                        context_assessments.c.available_at,
                        context_assessments.c.assessment_id,
                    )
                    .limit(limit)
                ).mappings()
            )
            assessment_ids = tuple(
                ContextAssessment.model_validate(row["assessment"]).assessment_id
                for row in rows
            )
            existing_keys = (
                set(
                    connection.execute(
                        select(
                            assessment_view_outcomes.c.assessment_id,
                            assessment_view_outcomes.c.asset,
                            assessment_view_outcomes.c.horizon_minutes,
                        ).where(
                            assessment_view_outcomes.c.assessment_id.in_(
                                assessment_ids
                            ),
                            assessment_view_outcomes.c.evaluation_version
                            == evaluation_version,
                        )
                    ).tuples()
                )
                if assessment_ids
                else set()
            )
        pending: list[PendingAssessmentView] = []
        for row in rows:
            assessment = ContextAssessment.model_validate(row["assessment"])
            packet = DecisionPacket.model_validate(row["packet"])
            self._require_packet_binding(assessment=assessment, packet=packet)
            state_by_asset = {item.asset: item for item in packet.asset_states}
            for view in assessment.views:
                if (assessment.assessment_id, view.asset, view.horizon_minutes) in (
                    existing_keys
                ):
                    continue
                state = state_by_asset[view.asset]
                pending.append(
                    PendingAssessmentView(
                        assessment=assessment,
                        packet=packet,
                        view=view,
                        symbol=state.market_symbol,
                    )
                )
                if len(pending) >= limit:
                    return tuple(pending)
        return tuple(pending)

    def record(self, outcome: AssessmentViewOutcome) -> bool:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    context_assessments.c.payload.label("assessment"),
                    decision_packets.c.payload.label("packet"),
                )
                .join(
                    decision_packets,
                    decision_packets.c.packet_id == context_assessments.c.packet_id,
                )
                .where(
                    context_assessments.c.assessment_id == outcome.assessment_id
                )
            ).mappings().one_or_none()
        if row is None:
            raise ValueError("AssessmentViewOutcome 缺少权威 Assessment")
        assessment = ContextAssessment.model_validate(row["assessment"])
        packet = DecisionPacket.model_validate(row["packet"])
        self._require_outcome_binding(
            outcome=outcome,
            assessment=assessment,
            packet=packet,
        )
        payload = outcome.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(assessment_view_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        assessment_id=outcome.assessment_id,
                        analysis_behavior_hash=outcome.analysis_behavior_hash,
                        asset=outcome.asset,
                        symbol=outcome.symbol,
                        horizon_minutes=outcome.horizon_minutes,
                        direction=outcome.direction.value,
                        already_priced=outcome.already_priced.value,
                        uncertainty=outcome.uncertainty.value,
                        evaluation_version=outcome.evaluation_version,
                        status=outcome.status.value,
                        signal_observed_at=outcome.signal_observed_at,
                        evaluation_at=outcome.evaluation_at,
                        settled_at=outcome.settled_at,
                        directional_return_bps=outcome.directional_return_bps,
                        payload=payload,
                    )
                )
        except IntegrityError:
            existing = self.outcome(outcome.outcome_id)
            if existing != outcome:
                raise ValueError("AssessmentViewOutcome 已存在且内容不同") from None
            return False
        return True

    def outcome(self, outcome_id: str) -> AssessmentViewOutcome | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(assessment_view_outcomes.c.payload).where(
                    assessment_view_outcomes.c.outcome_id == outcome_id
                )
            ).scalar_one_or_none()
        return None if payload is None else AssessmentViewOutcome.model_validate(payload)

    def visible_outcomes(
        self,
        *,
        analysis_behavior_hash: str,
        evaluation_version: str,
        signal_window_start: datetime,
        signal_window_end: datetime,
        published_at: datetime,
    ) -> tuple[AssessmentViewOutcome, ...]:
        start = _require_utc(signal_window_start)
        end = _require_utc(signal_window_end)
        published = _require_utc(published_at)
        if not start < end <= published:
            raise ValueError("Assessment outcome 查询窗口与发布时间顺序非法")
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(assessment_view_outcomes.c.payload)
                .where(
                    assessment_view_outcomes.c.analysis_behavior_hash
                    == analysis_behavior_hash,
                    assessment_view_outcomes.c.evaluation_version
                    == evaluation_version,
                    assessment_view_outcomes.c.signal_observed_at >= start,
                    assessment_view_outcomes.c.signal_observed_at < end,
                    assessment_view_outcomes.c.settled_at <= published,
                )
                .order_by(
                    assessment_view_outcomes.c.signal_observed_at,
                    assessment_view_outcomes.c.outcome_id,
                )
            ).scalars()
            return tuple(
                AssessmentViewOutcome.model_validate(payload) for payload in payloads
            )

    @staticmethod
    def _require_packet_binding(
        *,
        assessment: ContextAssessment,
        packet: DecisionPacket,
    ) -> None:
        if (
            assessment.decision_packet_hash != packet.content_hash
            or assessment.analysis_scope != packet.analysis_scope
            or assessment.mandate_version != packet.mandate_version
        ):
            raise ValueError("ContextAssessment 与 DecisionPacket 身份不一致")
        expected = tuple(
            (item.asset, item.horizon_minutes) for item in packet.required_views
        )
        observed = tuple(
            (item.asset, item.horizon_minutes) for item in assessment.views
        )
        if observed != expected:
            raise ValueError("ContextAssessment views 与 DecisionPacket 不一致")

    @classmethod
    def _require_outcome_binding(
        cls,
        *,
        outcome: AssessmentViewOutcome,
        assessment: ContextAssessment,
        packet: DecisionPacket,
    ) -> None:
        cls._require_packet_binding(assessment=assessment, packet=packet)
        view = next(
            (
                item
                for item in assessment.views
                if item.asset == outcome.asset
                and item.horizon_minutes == outcome.horizon_minutes
            ),
            None,
        )
        state = next(
            (item for item in packet.asset_states if item.asset == outcome.asset),
            None,
        )
        if view is None or state is None or (
            outcome.decision_packet_hash,
            outcome.analysis_scope,
            outcome.analysis_behavior_hash,
            outcome.symbol,
            outcome.direction,
            outcome.already_priced,
            outcome.uncertainty,
            outcome.signal_observed_at,
        ) != (
            packet.content_hash,
            assessment.analysis_scope,
            assessment.analysis_behavior_hash,
            state.market_symbol,
            view.direction,
            view.already_priced,
            view.uncertainty,
            assessment.available_at,
        ):
            raise ValueError("AssessmentViewOutcome 与权威 Assessment view 不一致")


@dataclass(slots=True)
class AssessmentViewOutcomeSettler:
    engine: Engine
    store: SqlAssessmentViewOutcomeStore
    evaluation_version: str
    maximum_market_age_seconds: int
    settlement_grace_minutes: int
    batch_size: int = 100

    def settle(self, *, as_of: datetime) -> AssessmentSettlementResult:
        now = _require_utc(as_of)
        settled = abstained = unscorable = pending_count = 0
        for pending in self.store.pending(
            evaluation_version=self.evaluation_version,
            limit=self.batch_size,
        ):
            assessment = pending.assessment
            view = pending.view
            evaluation_at = assessment.available_at + timedelta(
                minutes=view.horizon_minutes
            )
            if evaluation_at > now:
                pending_count += 1
                continue
            common = {
                "outcome_id": stable_id(
                    "assessment_view_outcome",
                    assessment.assessment_id,
                    view.asset,
                    view.horizon_minutes,
                    self.evaluation_version,
                ),
                "assessment_id": assessment.assessment_id,
                "decision_packet_hash": pending.packet.content_hash,
                "analysis_scope": assessment.analysis_scope,
                "analysis_behavior_hash": assessment.analysis_behavior_hash,
                "evaluation_version": self.evaluation_version,
                "asset": view.asset,
                "symbol": pending.symbol,
                "horizon_minutes": view.horizon_minutes,
                "direction": view.direction,
                "already_priced": view.already_priced,
                "uncertainty": view.uncertainty,
                "signal_observed_at": assessment.available_at,
                "evaluation_at": evaluation_at,
                "settled_at": now,
                "reference_price": None,
            }
            reference = trade_at_or_before(
                self.engine,
                symbol=pending.symbol,
                evaluation_at=assessment.available_at,
                visible_at=assessment.available_at,
            )
            if not self._fresh(
                trade=reference,
                expected_at=assessment.available_at,
            ):
                outcome = AssessmentViewOutcome(
                    **common,
                    status=ForecastOutcomeStatus.UNSCORABLE,
                    reason_code="REFERENCE_MARKET_DATA_MISSING_AT_ASSESSMENT_AVAILABILITY",
                )
                unscorable += int(self.store.record(outcome))
                continue
            assert reference is not None
            common["reference_price"] = reference.price
            exit_trade = trade_at_or_before(
                self.engine,
                symbol=pending.symbol,
                evaluation_at=evaluation_at,
                visible_at=now,
            )
            if self._fresh(trade=exit_trade, expected_at=evaluation_at):
                assert exit_trade is not None
                market_return = (
                    exit_trade.price / reference.price - Decimal("1")
                ) * Decimal("10000")
                if view.direction == DirectionalView.UNCERTAIN:
                    outcome = AssessmentViewOutcome(
                        **common,
                        status=ForecastOutcomeStatus.ABSTAINED,
                        exit_price=exit_trade.price,
                        exit_event_time=exit_trade.event_time,
                        market_return_bps=market_return,
                        reason_code="DIRECTIONAL_VIEW_ABSTAINED",
                    )
                    abstained += int(self.store.record(outcome))
                    continue
                directional_return = (
                    market_return
                    if view.direction == DirectionalView.UP
                    else -market_return
                )
                outcome = AssessmentViewOutcome(
                    **common,
                    status=ForecastOutcomeStatus.SETTLED,
                    exit_price=exit_trade.price,
                    exit_event_time=exit_trade.event_time,
                    market_return_bps=market_return,
                    directional_return_bps=directional_return,
                    direction_correct=directional_return > 0,
                    reason_code="DIRECTIONAL_RETURN_AVAILABLE",
                )
                settled += int(self.store.record(outcome))
                continue
            if now - evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                pending_count += 1
                continue
            outcome = AssessmentViewOutcome(
                **common,
                status=ForecastOutcomeStatus.UNSCORABLE,
                reason_code="MARKET_DATA_MISSING_AT_ASSESSMENT_HORIZON",
            )
            unscorable += int(self.store.record(outcome))
        return AssessmentSettlementResult(
            settled=settled,
            abstained=abstained,
            unscorable=unscorable,
            pending=pending_count,
        )

    def _fresh(self, *, trade, expected_at: datetime) -> bool:
        return trade is not None and timedelta(0) <= expected_at - trade.event_time <= (
            timedelta(seconds=self.maximum_market_age_seconds)
        )
