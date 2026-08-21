from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import and_, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.models import (
    BaseForecast,
    CalibratedForecast,
    ForecastKind,
    ForecastOutcome,
)
from investment_manager.forecast.tables import (
    context_assessments,
    forecast_outcomes,
    forecasts,
)
from investment_manager.kernel.time import require_utc

Forecast = BaseForecast | CalibratedForecast


def forecast_kind(forecast: Forecast) -> ForecastKind:
    return ForecastKind.BASE if isinstance(forecast, BaseForecast) else ForecastKind.CALIBRATED


def _forecast_from_row(kind: str, payload) -> Forecast:
    model = BaseForecast if ForecastKind(kind) == ForecastKind.BASE else CalibratedForecast
    return model.model_validate(payload)


class SqlForecastStore:
    """Immutable shared ledger for every program, AI, or hybrid forecast."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, forecast: Forecast) -> bool:
        kind = forecast_kind(forecast)
        try:
            with self._engine.begin() as connection:
                self._validate_dependencies(connection, forecast)
                connection.execute(
                    insert(forecasts).values(
                        forecast_id=forecast.forecast_id,
                        kind=kind.value,
                        producer_id=forecast.producer_id,
                        producer_version=forecast.producer_version,
                        forecast_family=forecast.forecast_family,
                        target_id=forecast.target.target_id,
                        available_at=forecast.available_at,
                        evaluation_at=forecast.available_at
                        + timedelta(minutes=forecast.horizon_minutes),
                        valid_until=forecast.valid_until,
                        base_forecast_id=(
                            forecast.base_forecast_id
                            if isinstance(forecast, CalibratedForecast)
                            else None
                        ),
                        assessment_id=(
                            forecast.assessment_id
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
                raise ValueError("forecast_id 已存在且 Forecast 内容不同") from None
            return False

    def forecast(self, forecast_id: str) -> Forecast | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(forecasts.c.kind, forecasts.c.payload).where(
                    forecasts.c.forecast_id == forecast_id
                )
            ).one_or_none()
        return None if row is None else _forecast_from_row(row.kind, row.payload)

    def pending(
        self,
        *,
        evaluation_version: str,
        limit: int,
        due_at: datetime | None = None,
    ) -> tuple[Forecast, ...]:
        if limit < 1:
            raise ValueError("Forecast pending limit 必须为正数")
        joined = forecasts.outerjoin(
            forecast_outcomes,
            and_(
                forecast_outcomes.c.forecast_id == forecasts.c.forecast_id,
                forecast_outcomes.c.evaluation_version == evaluation_version,
            ),
        )
        query = (
            select(forecasts.c.kind, forecasts.c.payload)
            .select_from(joined)
            .where(forecast_outcomes.c.outcome_id.is_(None))
        )
        if due_at is not None:
            query = query.where(forecasts.c.evaluation_at <= require_utc(due_at))
        with self._engine.connect() as connection:
            rows = connection.execute(
                query.order_by(forecasts.c.evaluation_at, forecasts.c.forecast_id).limit(limit)
            ).all()
        return tuple(_forecast_from_row(row.kind, row.payload) for row in rows)

    def record_outcome(self, outcome: ForecastOutcome) -> bool:
        try:
            with self._engine.begin() as connection:
                row = connection.execute(
                    select(forecasts.c.kind, forecasts.c.payload).where(
                        forecasts.c.forecast_id == outcome.forecast_id
                    )
                ).one_or_none()
                if row is None:
                    raise ValueError("ForecastOutcome 缺少权威 Forecast")
                forecast = _forecast_from_row(row.kind, row.payload)
                self._validate_outcome(forecast, outcome)
                connection.execute(
                    insert(forecast_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        forecast_id=outcome.forecast_id,
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
        producer_id: str,
        producer_version: str,
        evaluation_version: str,
    ) -> tuple[ForecastOutcome, ...]:
        joined = forecast_outcomes.join(
            forecasts,
            forecasts.c.forecast_id == forecast_outcomes.c.forecast_id,
        )
        with self._engine.connect() as connection:
            payloads: Iterable = connection.execute(
                select(forecast_outcomes.c.payload)
                .select_from(joined)
                .where(
                    forecasts.c.producer_id == producer_id,
                    forecasts.c.producer_version == producer_version,
                    forecast_outcomes.c.evaluation_version == evaluation_version,
                )
                .order_by(
                    forecast_outcomes.c.evaluation_at,
                    forecast_outcomes.c.forecast_id,
                )
            ).scalars()
            return tuple(ForecastOutcome.model_validate(item) for item in payloads)

    @staticmethod
    def _validate_dependencies(
        connection: Connection,
        forecast: Forecast,
    ) -> None:
        if not isinstance(forecast, CalibratedForecast):
            return
        if forecast.base_forecast_id is not None:
            base_kind = connection.execute(
                select(forecasts.c.kind).where(forecasts.c.forecast_id == forecast.base_forecast_id)
            ).scalar_one_or_none()
            if base_kind != ForecastKind.BASE.value:
                raise ValueError("PROGRAM_BASE/AI_ADJUSTED Forecast 必须引用已持久化 BaseForecast")
        if forecast.assessment_id is not None:
            assessment_exists = connection.execute(
                select(context_assessments.c.assessment_id).where(
                    context_assessments.c.assessment_id == forecast.assessment_id
                )
            ).scalar_one_or_none()
            if assessment_exists is None:
                raise ValueError("AI_EVENT/AI_ADJUSTED Forecast 必须引用已持久化 ContextAssessment")

    @staticmethod
    def _validate_outcome(
        forecast: Forecast,
        outcome: ForecastOutcome,
    ) -> None:
        expected_kind = forecast_kind(forecast)
        expected_legs = forecast.target.legs
        expected_leg_ids = tuple(item.instrument.key for item in expected_legs)
        observed_leg_ids = tuple(item.instrument_id for item in outcome.legs)
        if any(
            (
                outcome.forecast_kind != expected_kind,
                outcome.producer_id != forecast.producer_id,
                outcome.producer_version != forecast.producer_version,
                outcome.target_id != forecast.target.target_id,
                outcome.direction != forecast.direction,
                outcome.horizon_minutes != forecast.horizon_minutes,
                outcome.available_at != forecast.available_at,
            )
        ):
            raise ValueError("ForecastOutcome 与权威 Forecast 身份不一致")
        if outcome.legs and observed_leg_ids != expected_leg_ids:
            raise ValueError("ForecastOutcome 与权威 Forecast Leg 不一致")
        references = {item.instrument_id: item.price for item in forecast.reference_prices}
        if outcome.legs and any(
            (
                observed.direction != expected.direction
                or observed.gross_weight != expected.gross_weight
                or observed.reference_price != references[expected.instrument.key]
            )
            for observed, expected in zip(
                outcome.legs,
                expected_legs,
                strict=True,
            )
        ):
            raise ValueError("ForecastOutcome 与权威 Forecast Leg 事实不一致")
