from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

metadata = MetaData()
DATABASE_SCHEMA_VERSION = "v1a6e4c8b920"

# Immutable audit remnants from the retired physical CAP/CONTEXT split.  They
# have no repository or runtime consumer; declaring them here only prevents
# schema tooling from mistaking preserved history for drift.
historical_fact_store_identities = Table(
    "historical_fact_store_identities",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("store_id", String(64), nullable=False),
    Column("role", String(32), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
)
historical_fact_cohort_quarantines = Table(
    "historical_fact_cohort_quarantines",
    metadata,
    Column("quarantine_id", String(128), primary_key=True),
    Column("store_id", String(64), nullable=False),
    Column("manifest_id", String(128), nullable=False),
    Column("pipeline_id", String(128), nullable=False),
    Column("analysis_behavior_hash", String(64), nullable=True),
    Column("reason_code", String(64), nullable=False),
    Column("quarantined_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    return create_engine(database_url, echo=echo, future=True)


def require_current_schema(engine: Engine) -> None:
    """Fail closed before a runtime service touches an absent or stale schema."""

    try:
        with engine.connect() as connection:
            versions = tuple(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
    except SQLAlchemyError as exc:
        raise RuntimeError("数据库 Schema 版本不可读；拒绝启动运行服务") from exc
    if versions != (DATABASE_SCHEMA_VERSION,):
        observed = versions[0] if len(versions) == 1 else "MISSING_OR_MULTIPLE_HEADS"
        raise RuntimeError(
            f"数据库 Schema 版本不匹配：expected={DATABASE_SCHEMA_VERSION}, observed={observed}"
        )
