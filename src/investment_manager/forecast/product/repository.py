"""Immutable repository for deterministic product payoff projections."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.product.evaluation import ProductPayoffEvaluationCase
from investment_manager.forecast.product.models import (
    ProductPayoffOutcome,
    ProductPayoffProjection,
)
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastOutcome,
    ForecastResultKind,
)
from investment_manager.forecast.tables import (
    forecast_outcomes,
    forecasts,
    product_payoff_outcomes,
    product_payoff_projections,
)
from investment_manager.kernel.time import require_utc


class SqlProductPayoffProjectionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, projection: ProductPayoffProjection) -> bool:
        try:
            with self._engine.begin() as connection:
                self._validate_source(connection, projection)
                connection.execute(
                    insert(product_payoff_projections).values(
                        projection_id=projection.projection_id,
                        source_forecast_id=projection.source_forecast_id,
                        economic_exposure_id=projection.economic_exposure_id,
                        target_id=projection.target.target_id,
                        projected_at=projection.projected_at,
                        valid_until=projection.valid_until,
                        evaluation_at=projection.evaluation_at,
                        payload=projection.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.get(projection.projection_id)
            if existing != projection:
                raise ValueError("Product payoff projection 已存在且内容不同") from None
            return False

    def get(self, projection_id: str) -> ProductPayoffProjection | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(product_payoff_projections.c.payload).where(
                    product_payoff_projections.c.projection_id == projection_id
                )
            ).scalar_one_or_none()
        return (
            None
            if payload is None
            else ProductPayoffProjection.model_validate(payload)
        )

    def for_source(self, source_forecast_id: str) -> tuple[ProductPayoffProjection, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(product_payoff_projections.c.payload)
                .where(
                    product_payoff_projections.c.source_forecast_id
                    == source_forecast_id
                )
                .order_by(
                    product_payoff_projections.c.projected_at,
                    product_payoff_projections.c.target_id,
                )
            ).scalars()
            return tuple(ProductPayoffProjection.model_validate(item) for item in payloads)

    def pending_outcomes(
        self,
        *,
        evaluation_version: str,
        due_at: datetime,
        limit: int,
    ) -> tuple[ProductPayoffProjection, ...]:
        if limit < 1:
            raise ValueError("Product payoff pending outcome limit 必须为正数")
        joined = product_payoff_projections.outerjoin(
            product_payoff_outcomes,
            and_(
                product_payoff_outcomes.c.projection_id
                == product_payoff_projections.c.projection_id,
                product_payoff_outcomes.c.evaluation_version == evaluation_version,
            ),
        )
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(product_payoff_projections.c.payload)
                .select_from(joined)
                .where(
                    product_payoff_outcomes.c.outcome_id.is_(None),
                    product_payoff_projections.c.evaluation_at <= require_utc(due_at),
                )
                .order_by(
                    product_payoff_projections.c.evaluation_at,
                    product_payoff_projections.c.projection_id,
                )
                .limit(limit)
            ).scalars()
            return tuple(ProductPayoffProjection.model_validate(item) for item in payloads)

    def record_outcome(self, outcome: ProductPayoffOutcome) -> bool:
        try:
            with self._engine.begin() as connection:
                projection = self._projection(connection, outcome.projection_id)
                self._validate_outcome(projection, outcome)
                connection.execute(
                    insert(product_payoff_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        projection_id=outcome.projection_id,
                        source_forecast_id=outcome.source_forecast_id,
                        evaluation_version=outcome.evaluation_version,
                        status=outcome.status.value,
                        evaluation_at=outcome.evaluation_at,
                        settled_at=outcome.settled_at,
                        realized_gross_bps=outcome.realized_gross_bps,
                        payload=outcome.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.outcome(outcome.outcome_id)
            if existing != outcome:
                raise ValueError("ProductPayoffOutcome 已存在且内容不同") from None
            return False

    def outcome(self, outcome_id: str) -> ProductPayoffOutcome | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(product_payoff_outcomes.c.payload).where(
                    product_payoff_outcomes.c.outcome_id == outcome_id
                )
            ).scalar_one_or_none()
        return None if payload is None else ProductPayoffOutcome.model_validate(payload)

    def projection_outcomes(
        self,
        *,
        projection_ids: tuple[str, ...],
        evaluation_version: str,
    ) -> tuple[tuple[ProductPayoffProjection, ProductPayoffOutcome], ...]:
        """Load existing terminal outcomes for an exact candidate projection set."""

        if not projection_ids:
            return ()
        if tuple(sorted(set(projection_ids))) != projection_ids:
            raise ValueError("Product payoff outcome 查询 projection 必须唯一且排序")
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    product_payoff_projections.c.payload,
                    product_payoff_outcomes.c.payload,
                )
                .select_from(
                    product_payoff_projections.join(
                        product_payoff_outcomes,
                        and_(
                            product_payoff_outcomes.c.projection_id
                            == product_payoff_projections.c.projection_id,
                            product_payoff_outcomes.c.evaluation_version
                            == evaluation_version,
                        ),
                    )
                )
                .where(product_payoff_projections.c.projection_id.in_(projection_ids))
                .order_by(product_payoff_projections.c.projection_id)
            ).all()
        return tuple(
            (
                ProductPayoffProjection.model_validate(row[0]),
                ProductPayoffOutcome.model_validate(row[1]),
            )
            for row in rows
        )

    def outcome_cases(
        self,
        *,
        product_outcome_version: str,
        forecast_outcome_version: str,
        producer_behavior_id: str,
    ) -> tuple[ProductPayoffEvaluationCase, ...]:
        source_outcomes = forecast_outcomes.alias("source_outcomes")
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    forecasts.c.payload,
                    source_outcomes.c.payload,
                    product_payoff_projections.c.payload,
                    product_payoff_outcomes.c.payload,
                )
                .select_from(
                    product_payoff_outcomes.join(
                        product_payoff_projections,
                        product_payoff_projections.c.projection_id
                        == product_payoff_outcomes.c.projection_id,
                    ).join(
                        forecasts,
                        forecasts.c.forecast_id
                        == product_payoff_projections.c.source_forecast_id,
                    ).join(
                        source_outcomes,
                        and_(
                            source_outcomes.c.decision_slot_id
                            == forecasts.c.decision_slot_id,
                            source_outcomes.c.evaluation_version
                            == forecast_outcome_version,
                        ),
                    )
                )
                .where(
                    product_payoff_outcomes.c.evaluation_version
                    == product_outcome_version,
                    forecasts.c.producer_behavior_id == producer_behavior_id,
                )
                .order_by(
                    product_payoff_projections.c.evaluation_at,
                    product_payoff_projections.c.source_forecast_id,
                    product_payoff_projections.c.projection_id,
                )
            ).all()
        return tuple(
            ProductPayoffEvaluationCase(
                source_forecast=BaseForecast.model_validate(row[0]),
                source_outcome=ForecastOutcome.model_validate(row[1]),
                projection=ProductPayoffProjection.model_validate(row[2]),
                product_outcome=ProductPayoffOutcome.model_validate(row[3]),
            )
            for row in rows
        )

    @staticmethod
    def _projection(
        connection: Connection,
        projection_id: str,
    ) -> ProductPayoffProjection:
        payload = connection.execute(
            select(product_payoff_projections.c.payload).where(
                product_payoff_projections.c.projection_id == projection_id
            )
        ).scalar_one_or_none()
        if payload is None:
            raise ValueError("Product payoff outcome 缺少源 projection")
        return ProductPayoffProjection.model_validate(payload)

    @staticmethod
    def _validate_source(
        connection: Connection,
        projection: ProductPayoffProjection,
    ) -> None:
        row = connection.execute(
            select(forecasts.c.kind, forecasts.c.payload).where(
                forecasts.c.forecast_id == projection.source_forecast_id
            )
        ).one_or_none()
        if row is None or row.kind != ForecastResultKind.BASE.value:
            raise ValueError("Product projection 缺少已持久化 BaseForecast")
        source = BaseForecast.model_validate(row.payload)
        if (
            projection.source_contract_id != source.contract_id
            or projection.reference_instrument.key
            not in {leg.instrument.key for leg in source.target.legs}
            or projection.projected_at < source.available_at
            or projection.source_entry_valid_until != source.valid_until
            or projection.evaluation_at != source.economic_horizon_end
        ):
            raise ValueError("Product projection 与源 BaseForecast 不一致")

    @staticmethod
    def _validate_outcome(
        projection: ProductPayoffProjection,
        outcome: ProductPayoffOutcome,
    ) -> None:
        leg = projection.target.legs[0]
        if any(
            (
                outcome.source_forecast_id != projection.source_forecast_id,
                outcome.projected_at != projection.projected_at,
                outcome.evaluation_at != projection.evaluation_at,
                outcome.leg is not None
                and outcome.leg.instrument_id != leg.instrument.key,
                outcome.leg is not None
                and outcome.leg.direction != leg.direction,
                outcome.leg is not None
                and outcome.leg.reference_price != projection.entry_anchor.price,
            )
        ):
            raise ValueError("Product payoff outcome 与 projection 不一致")


__all__ = ["SqlProductPayoffProjectionStore"]
