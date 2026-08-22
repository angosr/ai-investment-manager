from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from investment_manager.platform.fact_store import (
    FactStoreRole,
    require_fact_store_role,
)
from investment_manager.schema import create_schema


def test_fact_store_role_is_claimed_once_and_survives_restart() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)

    require_fact_store_role(engine, FactStoreRole.CAPITAL, claim_if_missing=True)
    require_fact_store_role(engine, FactStoreRole.CAPITAL, claim_if_missing=False)

    with pytest.raises(RuntimeError, match="expected=CONTEXT, observed=CAPITAL"):
        require_fact_store_role(engine, FactStoreRole.CONTEXT, claim_if_missing=True)


def test_unclaimed_fact_store_is_not_silently_read_as_either_role() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)

    with pytest.raises(RuntimeError, match="尚未声明角色"):
        require_fact_store_role(engine, FactStoreRole.CONTEXT, claim_if_missing=False)
