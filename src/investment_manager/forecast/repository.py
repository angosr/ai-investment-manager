from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastSlotObligation,
)
from investment_manager.forecast.results import (
    BaseForecast,
    CalibratedForecast,
    Forecast,
    ForecastOutcome,
    ForecastOutcomeStatus,
    ForecastResultKind,
    forecast_kind,
)
from investment_manager.forecast.tables import (
    forecast_contracts,
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_outcomes,
    forecast_slot_obligations,
    forecasts,
)
from investment_manager.kernel.time import require_utc


def _forecast_from_row(kind: str, payload) -> Forecast:
    model = (
        BaseForecast if ForecastResultKind(kind) == ForecastResultKind.BASE else CalibratedForecast
    )
    return model.model_validate(payload)


class SqlForecastStore:
    """Immutable probability forecasts and source-independent slot outcomes."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, forecast: Forecast) -> bool:
        kind = forecast_kind(forecast)
        try:
            with self._engine.begin() as connection:
                contract, slot = self._validate_dependencies(connection, forecast)
                self._validate_against_contract(contract, slot, forecast)
                connection.execute(
                    insert(forecasts).values(
                        forecast_id=forecast.forecast_id,
                        kind=kind.value,
                        contract_id=forecast.contract_id,
                        decision_slot_id=forecast.decision_slot_id,
                        producer_id=forecast.producer_id,
                        producer_behavior_id=forecast.producer_behavior_id,
                        outcome_family_id=forecast.outcome_family_id,
                        target_id=forecast.target.target_id,
                        available_at=forecast.available_at,
                        valid_until=forecast.valid_until,
                        base_forecast_id=(
                            forecast.base_forecast_id
                            if isinstance(forecast, CalibratedForecast)
                            else None
                        ),
                        payload=forecast.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.forecast(forecast.forecast_id)
            if existing != forecast:
                raise ValueError("forecast_id/slot/behavior 已存在且内容不同") from None
            return False

    def forecast(self, forecast_id: str) -> Forecast | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(forecasts.c.kind, forecasts.c.payload).where(
                    forecasts.c.forecast_id == forecast_id
                )
            ).one_or_none()
        return None if row is None else _forecast_from_row(row.kind, row.payload)

    def result_for_behavior(
        self,
        *,
        decision_slot_id: str,
        producer_behavior_id: str,
    ) -> BaseForecast | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(forecasts.c.payload).where(
                    forecasts.c.kind == ForecastResultKind.BASE.value,
                    forecasts.c.decision_slot_id == decision_slot_id,
                    forecasts.c.producer_behavior_id == producer_behavior_id,
                )
            ).scalar_one_or_none()
        return None if payload is None else BaseForecast.model_validate(payload)

    def no_estimate_exists(
        self,
        *,
        decision_slot_id: str,
        producer_behavior_id: str,
    ) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    select(forecast_no_estimates.c.result_id).where(
                        forecast_no_estimates.c.slot_id == decision_slot_id,
                        forecast_no_estimates.c.producer_behavior_id == producer_behavior_id,
                    )
                ).scalar_one_or_none()
                is not None
            )

    def latest_base_for_target(
        self,
        *,
        target_id: str,
        outcome_family_id: str,
        as_of: datetime,
        include_expired: bool = False,
    ) -> BaseForecast | None:
        payload = self._latest_payload(
            kind=ForecastResultKind.BASE,
            target_id=target_id,
            outcome_family_id=outcome_family_id,
            as_of=as_of,
            include_expired=include_expired,
        )
        return None if payload is None else BaseForecast.model_validate(payload)

    def latest_calibrated_for_target(
        self,
        *,
        target_id: str,
        outcome_family_id: str,
        as_of: datetime,
        include_expired: bool = False,
    ) -> CalibratedForecast | None:
        payload = self._latest_payload(
            kind=ForecastResultKind.CALIBRATED,
            target_id=target_id,
            outcome_family_id=outcome_family_id,
            as_of=as_of,
            include_expired=include_expired,
        )
        return None if payload is None else CalibratedForecast.model_validate(payload)

    def _latest_payload(
        self,
        *,
        kind: ForecastResultKind,
        target_id: str,
        outcome_family_id: str,
        as_of: datetime,
        include_expired: bool,
    ):
        now = require_utc(as_of)
        with self._engine.connect() as connection:
            query = select(forecasts.c.payload).where(
                forecasts.c.kind == kind.value,
                forecasts.c.target_id == target_id,
                forecasts.c.outcome_family_id == outcome_family_id,
                forecasts.c.available_at <= now,
            )
            if not include_expired:
                query = query.where(forecasts.c.valid_until > now)
            return connection.execute(
                query.order_by(
                    forecasts.c.available_at.desc(),
                    forecasts.c.forecast_id.desc(),
                ).limit(1)
            ).scalar_one_or_none()

    def active_base_forecasts(
        self,
        *,
        producer_id: str,
        producer_behavior_id: str,
        outcome_family_id: str,
        as_of: datetime,
        limit: int = 100,
    ) -> tuple[BaseForecast, ...]:
        if limit < 1:
            raise ValueError("active BaseForecast limit 必须为正数")
        now = require_utc(as_of)
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(forecasts.c.payload)
                .where(
                    forecasts.c.kind == ForecastResultKind.BASE.value,
                    forecasts.c.producer_id == producer_id,
                    forecasts.c.producer_behavior_id == producer_behavior_id,
                    forecasts.c.outcome_family_id == outcome_family_id,
                    forecasts.c.available_at <= now,
                    forecasts.c.valid_until > now,
                )
                .order_by(forecasts.c.available_at, forecasts.c.forecast_id)
                .limit(limit)
            ).scalars()
            return tuple(BaseForecast.model_validate(item) for item in payloads)

    def pending_slots(
        self,
        *,
        evaluation_version: str,
        limit: int,
        due_at: datetime | None = None,
    ) -> tuple[tuple[ForecastContract, ForecastDecisionSlot], ...]:
        if limit < 1:
            raise ValueError("Forecast pending slot limit 必须为正数")
        joined = forecast_decision_slots.join(
            forecast_contracts,
            forecast_contracts.c.contract_id == forecast_decision_slots.c.contract_id,
        ).outerjoin(
            forecast_outcomes,
            and_(
                forecast_outcomes.c.decision_slot_id == forecast_decision_slots.c.slot_id,
                forecast_outcomes.c.evaluation_version == evaluation_version,
            ),
        )
        query = (
            select(forecast_contracts.c.payload, forecast_decision_slots.c.payload)
            .select_from(joined)
            .where(forecast_outcomes.c.outcome_id.is_(None))
        )
        if due_at is not None:
            query = query.where(forecast_decision_slots.c.evaluation_at <= require_utc(due_at))
        with self._engine.connect() as connection:
            rows = connection.execute(
                query.order_by(
                    forecast_decision_slots.c.evaluation_at,
                    forecast_decision_slots.c.slot_id,
                ).limit(limit)
            ).all()
        return tuple(
            (
                ForecastContract.model_validate(row[0]),
                ForecastDecisionSlot.model_validate(row[1]),
            )
            for row in rows
        )

    def record_outcome(self, outcome: ForecastOutcome) -> bool:
        try:
            with self._engine.begin() as connection:
                contract, slot = self._contract_and_slot(
                    connection,
                    contract_id=outcome.contract_id,
                    slot_id=outcome.decision_slot_id,
                )
                self._validate_outcome(contract, slot, outcome)
                connection.execute(
                    insert(forecast_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        contract_id=outcome.contract_id,
                        decision_slot_id=outcome.decision_slot_id,
                        evaluation_version=outcome.evaluation_version,
                        status=outcome.status.value,
                        evaluation_at=outcome.evaluation_at,
                        settled_at=outcome.settled_at,
                        gross_target_return_bps=outcome.gross_target_return_bps,
                        payload=outcome.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.outcome(outcome.outcome_id)
            if existing != outcome:
                raise ValueError("ForecastOutcome 已存在且内容不同") from None
            return False

    def outcome(self, outcome_id: str) -> ForecastOutcome | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(forecast_outcomes.c.payload).where(
                    forecast_outcomes.c.outcome_id == outcome_id
                )
            ).scalar_one_or_none()
        return None if payload is None else ForecastOutcome.model_validate(payload)

    def outcomes(
        self,
        *,
        contract_id: str,
        evaluation_version: str,
    ) -> tuple[ForecastOutcome, ...]:
        with self._engine.connect() as connection:
            payloads: Iterable = connection.execute(
                select(forecast_outcomes.c.payload)
                .where(
                    forecast_outcomes.c.contract_id == contract_id,
                    forecast_outcomes.c.evaluation_version == evaluation_version,
                )
                .order_by(
                    forecast_outcomes.c.evaluation_at,
                    forecast_outcomes.c.decision_slot_id,
                )
            ).scalars()
            return tuple(ForecastOutcome.model_validate(item) for item in payloads)

    @staticmethod
    def _validate_dependencies(
        connection: Connection,
        forecast: Forecast,
    ) -> tuple[ForecastContract, ForecastDecisionSlot]:
        contract, slot = SqlForecastStore._contract_and_slot(
            connection,
            contract_id=forecast.contract_id,
            slot_id=forecast.decision_slot_id,
        )
        absence = connection.execute(
            select(forecast_no_estimates.c.result_id).where(
                forecast_no_estimates.c.slot_id == forecast.decision_slot_id,
                forecast_no_estimates.c.producer_behavior_id == forecast.producer_behavior_id,
            )
        ).scalar_one_or_none()
        if absence is not None:
            raise ValueError("同一 decision slot/producer 已记录 NO_ESTIMATE")
        obligation_payload = connection.execute(
            select(forecast_slot_obligations.c.payload).where(
                forecast_slot_obligations.c.slot_id == forecast.decision_slot_id,
                forecast_slot_obligations.c.producer_behavior_id
                == forecast.producer_behavior_id,
            )
        ).scalar_one_or_none()
        if obligation_payload is None:
            raise ValueError("Forecast 缺少事前槽义务")
        obligation = ForecastSlotObligation.model_validate(obligation_payload)
        if (
            obligation.contract_id != forecast.contract_id
            or obligation.producer_id != forecast.producer_id
        ):
            raise ValueError("Forecast 与槽义务身份不一致")
        if isinstance(forecast, CalibratedForecast):
            base = connection.execute(
                select(forecasts.c.kind, forecasts.c.payload).where(
                    forecasts.c.forecast_id == forecast.base_forecast_id
                )
            ).one_or_none()
            if base is None or base.kind != ForecastResultKind.BASE.value:
                raise ValueError("CalibratedForecast 缺少已持久化 BaseForecast")
            authoritative = BaseForecast.model_validate(base.payload)
            if (
                authoritative.contract_id != forecast.contract_id
                or authoritative.decision_slot_id != forecast.decision_slot_id
                or authoritative.producer_behavior_id != forecast.producer_behavior_id
            ):
                raise ValueError("CalibratedForecast 与 BaseForecast 身份不一致")
        return contract, slot

    @staticmethod
    def _contract_and_slot(
        connection: Connection,
        *,
        contract_id: str,
        slot_id: str,
    ) -> tuple[ForecastContract, ForecastDecisionSlot]:
        row = connection.execute(
            select(forecast_contracts.c.payload, forecast_decision_slots.c.payload)
            .select_from(
                forecast_decision_slots.join(
                    forecast_contracts,
                    forecast_contracts.c.contract_id == forecast_decision_slots.c.contract_id,
                )
            )
            .where(
                forecast_contracts.c.contract_id == contract_id,
                forecast_decision_slots.c.slot_id == slot_id,
            )
        ).one_or_none()
        if row is None:
            raise ValueError("Forecast 缺少匹配的 Contract/DecisionSlot")
        return (
            ForecastContract.model_validate(row[0]),
            ForecastDecisionSlot.model_validate(row[1]),
        )

    @staticmethod
    def _validate_against_contract(
        contract: ForecastContract,
        slot: ForecastDecisionSlot,
        forecast: Forecast,
    ) -> None:
        if any(
            (
                forecast.outcome_family_id != contract.outcome_family_id,
                forecast.target != contract.target,
                forecast.horizon_minutes != contract.horizon_minutes,
                forecast.orientation not in contract.allowed_orientations,
                forecast.information_cutoff_at != slot.information_cutoff_at,
                forecast.cutoff_prices != slot.cutoff_prices,
                forecast.available_at > slot.completion_deadline_at,
            )
        ):
            raise ValueError("Forecast 与 ForecastContract/DecisionSlot 不一致")
        expected_buckets = tuple(item.bucket_id for item in contract.outcome_buckets)
        observed_buckets = tuple(item.bucket_id for item in forecast.outcome_probabilities)
        if observed_buckets != expected_buckets:
            raise ValueError("Forecast 概率未完整覆盖合同 buckets")
        expected_gross = sum(
            (
                probability.probability * bucket.representative_bps
                for probability, bucket in zip(
                    forecast.outcome_probabilities,
                    contract.outcome_buckets,
                    strict=True,
                )
            ),
            start=Decimal("0"),
        )
        if forecast.expected_gross_bps != expected_gross:
            raise ValueError("Forecast expected_gross_bps 必须由合同分布确定")

    @staticmethod
    def _validate_outcome(
        contract: ForecastContract,
        slot: ForecastDecisionSlot,
        outcome: ForecastOutcome,
    ) -> None:
        if (
            outcome.information_cutoff_at != slot.information_cutoff_at
            or outcome.outcome_start_at != slot.outcome_start_at
            or outcome.evaluation_at != slot.evaluation_at
        ):
            raise ValueError("ForecastOutcome 与权威 DecisionSlot 时间不一致")
        if outcome.status == ForecastOutcomeStatus.SETTLED:
            if tuple(item.instrument_id for item in outcome.legs) != tuple(
                item.instrument.key for item in contract.target.legs
            ):
                raise ValueError("ForecastOutcome 与权威 ForecastContract Leg 不一致")
            bucket = next(
                (
                    item
                    for item in contract.outcome_buckets
                    if (item.lower_bps is None or outcome.gross_target_return_bps >= item.lower_bps)
                    and (item.upper_bps is None or outcome.gross_target_return_bps < item.upper_bps)
                ),
                None,
            )
            if bucket is None or bucket.bucket_id != outcome.realized_bucket_id:
                raise ValueError("ForecastOutcome realized bucket 与合同不一致")
