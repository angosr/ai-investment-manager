from __future__ import annotations

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

metadata = MetaData()
DATABASE_SCHEMA_VERSION = "d4b7e2c9a106"


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
