"""Point-in-time ledger for product order facts observed from a venue."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import field_validator, model_validator
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.tables import product_order_observations
from investment_manager.execution.venue.product import ProductOrder
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.time import database_utc


class ProductOrderObservation(FrozenModel):
    observation_id: str
    observation_hash: str
    available_at: datetime
    order: ProductOrder

    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def identity_must_match_order_fact(self):
        expected_hash = content_hash(self.order)
        if self.observation_hash != expected_hash:
            raise ValueError("产品订单 observation_hash 与事实不一致")
        if self.observation_id != stable_id("order_observation", expected_hash):
            raise ValueError("产品订单 observation_id 与事实不一致")
        return self


class ProductOrderObservationStore(Protocol):
    def record(self, order: ProductOrder, *, available_at: datetime) -> bool: ...

    def for_group(
        self,
        group_id: str,
        *,
        as_of: datetime,
    ) -> tuple[ProductOrderObservation, ...]: ...

    def for_groups(
        self,
        group_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> dict[str, tuple[ProductOrderObservation, ...]]: ...

    def history_for_groups(
        self,
        group_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> dict[str, tuple[ProductOrderObservation, ...]]: ...


class SqlProductOrderObservationStore:
    """Keep the earliest system visibility time for each distinct venue fact."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, order: ProductOrder, *, available_at: datetime) -> bool:
        available_at = require_utc(available_at)
        observation_hash = content_hash(order)
        observation_id = stable_id("order_observation", observation_hash)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(product_order_observations).values(
                        observation_id=observation_id,
                        observation_hash=observation_hash,
                        client_order_id=order.client_order_id,
                        group_id=order.group_id,
                        available_at=available_at,
                        payload=order.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(product_order_observations.c.payload).where(
                        product_order_observations.c.observation_id == observation_id
                    )
                ).scalar_one_or_none()
            if row is None or ProductOrder.model_validate(row) != order:
                raise ValueError("产品订单观察 identity 已存在且事实不同") from None
            return False

    def for_group(
        self,
        group_id: str,
        *,
        as_of: datetime,
    ) -> tuple[ProductOrderObservation, ...]:
        return self.for_groups((group_id,), as_of=as_of)[group_id]

    def for_groups(
        self,
        group_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> dict[str, tuple[ProductOrderObservation, ...]]:
        history = self.history_for_groups(group_ids, as_of=as_of)
        result: dict[str, tuple[ProductOrderObservation, ...]] = {}
        for group_id, observations in history.items():
            latest: dict[str, ProductOrderObservation] = {}
            for observation in observations:
                latest[observation.order.client_order_id] = observation
            result[group_id] = tuple(latest[key] for key in sorted(latest))
        return result

    def history_for_groups(
        self,
        group_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> dict[str, tuple[ProductOrderObservation, ...]]:
        as_of = require_utc(as_of)
        group_ids = tuple(sorted(set(group_ids)))
        if not group_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    product_order_observations.c.group_id,
                    product_order_observations.c.available_at,
                    product_order_observations.c.observation_id,
                    product_order_observations.c.observation_hash,
                    product_order_observations.c.payload,
                )
                .where(
                    product_order_observations.c.group_id.in_(group_ids),
                    product_order_observations.c.available_at <= as_of,
                )
                .order_by(
                    product_order_observations.c.available_at,
                    product_order_observations.c.observation_id,
                )
            ).all()
        history: dict[str, list[ProductOrderObservation]] = {
            group_id: [] for group_id in group_ids
        }
        for row in rows:
            order = ProductOrder.model_validate(row.payload)
            history[row.group_id].append(
                ProductOrderObservation(
                    observation_id=row.observation_id,
                    observation_hash=row.observation_hash,
                    available_at=database_utc(row.available_at),
                    order=order,
                )
            )
        return {
            group_id: tuple(
                sorted(
                    observations,
                    key=lambda item: (
                        item.available_at,
                        item.order.observed_at,
                        item.order.filled_quantity,
                        item.observation_id,
                    ),
                )
            )
            for group_id, observations in history.items()
        }
