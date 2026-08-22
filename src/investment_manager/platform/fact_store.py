from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, String, Table, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.platform.database import metadata


class FactStoreRole(StrEnum):
    CAPITAL = "CAPITAL"
    CONTEXT = "CONTEXT"


fact_store_identity = Table(
    "fact_store_identity",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("store_id", String(64), nullable=False, unique=True),
    Column("role", String(32), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("singleton_key = 'PRIMARY'", name="ck_fact_store_singleton"),
    CheckConstraint("role IN ('CAPITAL', 'CONTEXT')", name="ck_fact_store_role"),
)


def require_fact_store_role(
    engine: Engine,
    expected_role: FactStoreRole,
    *,
    claim_if_missing: bool,
) -> None:
    """Bind or verify one physical fact store before domain services use it."""

    with engine.connect() as connection:
        row = connection.execute(select(fact_store_identity)).mappings().one_or_none()
    if row is None and claim_if_missing:
        try:
            with engine.begin() as connection:
                connection.execute(
                    insert(fact_store_identity).values(
                        singleton_key="PRIMARY",
                        store_id=uuid4().hex,
                        role=expected_role.value,
                        claimed_at=datetime.now(UTC),
                    )
                )
        except IntegrityError:
            # A concurrent process may have claimed the singleton first.
            pass
        with engine.connect() as connection:
            row = connection.execute(select(fact_store_identity)).mappings().one_or_none()
    if row is None:
        raise RuntimeError("事实库尚未声明角色；拒绝读取运行事实")
    observed = row["role"]
    if observed != expected_role.value:
        raise RuntimeError(
            f"事实库角色不匹配：expected={expected_role.value}, observed={observed}"
        )
