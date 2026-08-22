from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from investment_manager.platform.fact_store import (
    FactStoreRole,
    SqlFactCohortQuarantineStore,
    build_fact_cohort_quarantine,
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


def test_wrong_store_cohort_quarantine_is_append_only_and_store_bound() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    require_fact_store_role(engine, FactStoreRole.CAPITAL, claim_if_missing=True)
    store = SqlFactCohortQuarantineStore(engine)
    store_id, observed_role = store.current_identity()
    quarantine = build_fact_cohort_quarantine(
        store_id=store_id,
        observed_role=observed_role,
        expected_role=FactStoreRole.CONTEXT,
        manifest_id="release-context-v1",
        pipeline_id="context-v1",
        analysis_behavior_hash="a" * 64,
        quarantined_at=datetime(2026, 8, 22, tzinfo=UTC),
        evidence_ref="review-finding-v1",
    )

    assert store.record(quarantine) == quarantine
    assert store.record(quarantine) == quarantine

    wrong_store = build_fact_cohort_quarantine(
        store_id="different-store",
        observed_role=FactStoreRole.CAPITAL,
        expected_role=FactStoreRole.CONTEXT,
        manifest_id="release-context-v1",
        pipeline_id="context-v1",
        analysis_behavior_hash="a" * 64,
        quarantined_at=datetime(2026, 8, 22, tzinfo=UTC),
        evidence_ref="review-finding-v1",
    )
    with pytest.raises(RuntimeError, match="物理事实库身份"):
        store.record(wrong_store)
