from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import JSON, CheckConstraint, Column, DateTime, String, Table, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.kernel.identity import SHA256_PATTERN, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
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

fact_cohort_quarantines = Table(
    "fact_cohort_quarantines",
    metadata,
    Column("quarantine_id", String(128), primary_key=True),
    Column("store_id", String(64), nullable=False),
    Column("manifest_id", String(128), nullable=False),
    Column("pipeline_id", String(128), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=True),
    Column("reason_code", String(64), nullable=False),
    Column("quarantined_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    CheckConstraint(
        "reason_code = 'WRONG_FACT_STORE'",
        name="ck_fact_cohort_quarantine_reason",
    ),
)


class FactCohortQuarantine(FrozenModel):
    """Append-only exclusion of one producer cohort written to the wrong store."""

    quarantine_id: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    observed_role: FactStoreRole
    expected_role: FactStoreRole
    manifest_id: str = Field(min_length=1)
    pipeline_id: str = Field(min_length=1)
    analysis_behavior_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    reason_code: Literal["WRONG_FACT_STORE"] = "WRONG_FACT_STORE"
    quarantined_at: datetime
    evidence_ref: str = Field(min_length=1, max_length=500)

    _utc_quarantined_at = field_validator("quarantined_at")(require_utc)

    @model_validator(mode="after")
    def roles_and_identity_are_consistent(self):
        if self.observed_role == self.expected_role:
            raise ValueError("错库隔离的实际角色与预期角色不能相同")
        expected = stable_id(
            "fact_cohort_quarantine",
            self.store_id,
            self.manifest_id,
            self.pipeline_id,
            self.analysis_behavior_hash,
            self.reason_code,
        )
        if self.quarantine_id != expected:
            raise ValueError("FactCohortQuarantine 身份与目标 cohort 不一致")
        return self


def build_fact_cohort_quarantine(
    *,
    store_id: str,
    observed_role: FactStoreRole,
    expected_role: FactStoreRole,
    manifest_id: str,
    pipeline_id: str,
    analysis_behavior_hash: str | None,
    quarantined_at: datetime,
    evidence_ref: str,
) -> FactCohortQuarantine:
    quarantine_id = stable_id(
        "fact_cohort_quarantine",
        store_id,
        manifest_id,
        pipeline_id,
        analysis_behavior_hash,
        "WRONG_FACT_STORE",
    )
    return FactCohortQuarantine(
        quarantine_id=quarantine_id,
        store_id=store_id,
        observed_role=observed_role,
        expected_role=expected_role,
        manifest_id=manifest_id,
        pipeline_id=pipeline_id,
        analysis_behavior_hash=analysis_behavior_hash,
        quarantined_at=quarantined_at,
        evidence_ref=evidence_ref,
    )


class SqlFactCohortQuarantineStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, quarantine: FactCohortQuarantine) -> FactCohortQuarantine:
        with self._engine.connect() as connection:
            identity = connection.execute(select(fact_store_identity)).mappings().one_or_none()
        if identity is None:
            raise RuntimeError("事实库尚未声明角色；拒绝追加 cohort 隔离事实")
        if (
            identity["store_id"] != quarantine.store_id
            or identity["role"] != quarantine.observed_role.value
        ):
            raise RuntimeError("cohort 隔离事实与物理事实库身份不一致")
        payload = quarantine.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(fact_cohort_quarantines).values(
                        quarantine_id=quarantine.quarantine_id,
                        store_id=quarantine.store_id,
                        manifest_id=quarantine.manifest_id,
                        pipeline_id=quarantine.pipeline_id,
                        analysis_behavior_hash=quarantine.analysis_behavior_hash,
                        reason_code=quarantine.reason_code,
                        quarantined_at=quarantine.quarantined_at,
                        payload=payload,
                    )
                )
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(fact_cohort_quarantines.c.payload).where(
                        fact_cohort_quarantines.c.quarantine_id
                        == quarantine.quarantine_id
                    )
                ).scalar_one_or_none()
            if existing != payload:
                raise ValueError("相同 cohort 隔离身份对应不同内容") from None
        return quarantine

    def current_identity(self) -> tuple[str, FactStoreRole]:
        with self._engine.connect() as connection:
            row = connection.execute(select(fact_store_identity)).mappings().one_or_none()
        if row is None:
            raise RuntimeError("事实库尚未声明角色")
        return row["store_id"], FactStoreRole(row["role"])


def pipeline_not_quarantined(column):
    return ~column.in_(select(fact_cohort_quarantines.c.pipeline_id))


def manifest_not_quarantined(column):
    return ~column.in_(select(fact_cohort_quarantines.c.manifest_id))


def analysis_behavior_not_quarantined(column):
    return ~column.in_(
        select(fact_cohort_quarantines.c.analysis_behavior_hash).where(
            fact_cohort_quarantines.c.analysis_behavior_hash.is_not(None)
        )
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
