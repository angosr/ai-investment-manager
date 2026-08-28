from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.cash.models import CashYieldProductObservation
from investment_manager.execution.tables import cash_yield_product_observations
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc


class CashYieldObservationStore(Protocol):
    def put(self, observation: CashYieldProductObservation) -> bool: ...

    def latest(
        self,
        *,
        product_id: str,
        asset: str,
        visible_at: datetime,
    ) -> CashYieldProductObservation | None: ...


class SqlCashYieldObservationStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, observation: CashYieldProductObservation) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(cash_yield_product_observations).values(
                        observation_id=observation.observation_id,
                        product_id=observation.product_id,
                        asset=observation.asset,
                        available_at=observation.available_at,
                        observation_hash=content_hash(observation),
                        payload=observation.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.get(observation.observation_id)
            if existing != observation:
                raise ValueError("现金收益产品观察身份已存在且内容不同") from None
            return False

    def get(self, observation_id: str) -> CashYieldProductObservation | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(cash_yield_product_observations.c.payload).where(
                    cash_yield_product_observations.c.observation_id == observation_id
                )
            ).scalar_one_or_none()
        return None if payload is None else CashYieldProductObservation.model_validate(payload)

    def latest(
        self,
        *,
        product_id: str,
        asset: str,
        visible_at: datetime,
    ) -> CashYieldProductObservation | None:
        cutoff = require_utc(visible_at)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(cash_yield_product_observations.c.payload)
                .where(
                    cash_yield_product_observations.c.product_id == product_id,
                    cash_yield_product_observations.c.asset == asset,
                    cash_yield_product_observations.c.available_at <= cutoff,
                )
                .order_by(
                    cash_yield_product_observations.c.available_at.desc(),
                    cash_yield_product_observations.c.observation_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else CashYieldProductObservation.model_validate(payload)
