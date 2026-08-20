from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from quant_core.asset_management import Materiality
from quant_core.fact_pipeline import (
    FOMC_MEETING_FACT_TYPE,
    FactDeltaPolicy,
    FactDeltaRule,
    OfficialFactProjectionPolicy,
    build_fact_material_delta,
    build_state_snapshot,
    project_fomc_calendar_fact,
)
from quant_core.fact_state_sql import SqlFactStateStore
from quant_core.official_information import parse_fomc_calendar
from quant_core.official_information_sql import SqlOfficialInformationStore
from quant_core.persistence import (
    canonical_fact_revision_sources,
    canonical_fact_revisions,
    create_schema,
    material_deltas,
    state_snapshots,
)

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
FACT_POLICY = OfficialFactProjectionPolicy(
    version="fed-fact-v1",
    affected_assets=("BTC", "ETH"),
)
DELTA_POLICY = FactDeltaPolicy(
    version="fact-delta-v1",
    validity_seconds=3_600,
    horizons_minutes=(60, 240),
    rules=(
        FactDeltaRule(
            fact_type=FOMC_MEETING_FACT_TYPE,
            materiality=Materiality.NORMAL,
            reason_code="FOMC_SCHEDULE_REVISION",
        ),
    ),
)


def _record(date_text: str, *, observed_at: datetime):
    return parse_fomc_calendar(
        f"""
        <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">{date_text}</div>
        </div>
        """,
        observed_at=observed_at,
    )[0]


def _stores():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine, SqlOfficialInformationStore(engine), SqlFactStateStore(engine)


def test_fact_revision_store_is_append_only_and_point_in_time_visible() -> None:
    engine, official_store, fact_store = _stores()
    first_calendar = official_store.put(
        _record("15-16", observed_at=OBSERVED_AT)
    ).calendar_revision
    assert first_calendar is not None
    first = project_fomc_calendar_fact(first_calendar, policy=FACT_POLICY)
    fact_store.put_fact(first)

    second_at = OBSERVED_AT + timedelta(minutes=1)
    second_calendar = official_store.put(
        _record("16-17", observed_at=second_at)
    ).calendar_revision
    assert second_calendar is not None
    second = project_fomc_calendar_fact(
        second_calendar,
        policy=FACT_POLICY,
        previous=first,
    )
    fact_store.put_fact(second)
    assert fact_store.put_fact(second) == second

    assert fact_store.facts_as_of(as_of=OBSERVED_AT) == (first,)
    assert fact_store.facts_as_of(as_of=second_at) == (second,)
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(canonical_fact_revisions)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(canonical_fact_revision_sources)
        ) == 2


def test_fact_requires_persisted_source_observation() -> None:
    engine, _, fact_store = _stores()
    record = _record("15-16", observed_at=OBSERVED_AT)
    from quant_core.official_information import build_fomc_calendar_revision

    fact = project_fomc_calendar_fact(
        build_fomc_calendar_revision(record),
        policy=FACT_POLICY,
    )

    with pytest.raises(ValueError, match="来源观测"):
        fact_store.put_fact(fact)
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(canonical_fact_revisions)
        ) == 0


def test_fact_cannot_be_visible_before_its_source_observation() -> None:
    engine, official_store, fact_store = _stores()
    calendar = official_store.put(
        _record("15-16", observed_at=OBSERVED_AT)
    ).calendar_revision
    assert calendar is not None
    fact = project_fomc_calendar_fact(calendar, policy=FACT_POLICY).model_copy(
        update={"observed_at": OBSERVED_AT - timedelta(seconds=1)}
    )

    with pytest.raises(ValueError, match="截至 observed_at 可见"):
        fact_store.put_fact(fact)
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(canonical_fact_revisions)
        ) == 0


def test_state_and_delta_transition_is_atomic_idempotent_and_replayable() -> None:
    engine, official_store, fact_store = _stores()
    first_calendar = official_store.put(
        _record("15-16", observed_at=OBSERVED_AT)
    ).calendar_revision
    assert first_calendar is not None
    first_fact = project_fomc_calendar_fact(first_calendar, policy=FACT_POLICY)
    fact_store.put_fact(first_fact)
    first_state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(first_fact,),
    )
    fact_store.put_state(first_state)

    second_at = OBSERVED_AT + timedelta(minutes=1)
    second_calendar = official_store.put(
        _record("16-17", observed_at=second_at)
    ).calendar_revision
    assert second_calendar is not None
    second_fact = project_fomc_calendar_fact(
        second_calendar,
        policy=FACT_POLICY,
        previous=first_fact,
    )
    fact_store.put_fact(second_fact)
    second_state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=second_at,
        built_at=second_at,
        facts=(second_fact,),
    )
    delta = build_fact_material_delta(
        previous=first_state,
        current=second_state,
        current_facts=(second_fact,),
        policy=DELTA_POLICY,
    )
    assert delta is not None

    first_write = fact_store.record_transition(state=second_state, delta=delta)
    replayed = fact_store.record_transition(
        state=second_state.model_copy(
            update={"built_at": second_at + timedelta(seconds=30)}
        ),
        delta=delta,
    )

    assert first_write == (second_state, delta)
    assert replayed == (second_state, delta)
    assert fact_store.latest_state(
        analysis_scope="crypto-macro",
        projection_version="state-v1",
        as_of=OBSERVED_AT,
    ) == first_state
    assert fact_store.latest_state(
        analysis_scope="crypto-macro",
        projection_version="state-v1",
        as_of=second_at,
    ) == second_state
    assert fact_store.delta(delta.delta_id) == delta
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(state_snapshots)) == 2
        assert connection.scalar(select(func.count()).select_from(material_deltas)) == 1


def test_failed_delta_rolls_back_new_state() -> None:
    _, official_store, fact_store = _stores()
    calendar = official_store.put(
        _record("15-16", observed_at=OBSERVED_AT)
    ).calendar_revision
    assert calendar is not None
    fact = project_fomc_calendar_fact(calendar, policy=FACT_POLICY)
    fact_store.put_fact(fact)
    missing_previous = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT - timedelta(seconds=1),
        built_at=OBSERVED_AT,
        facts=(),
    )
    current = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(fact,),
    )
    delta = build_fact_material_delta(
        previous=missing_previous,
        current=current,
        current_facts=(fact,),
        policy=DELTA_POLICY,
    )
    assert delta is not None

    with pytest.raises(ValueError, match="StateSnapshot 不完整"):
        fact_store.record_transition(state=current, delta=delta)

    assert fact_store.state(current.state_id) is None


def test_state_store_rejects_tampered_content_identity() -> None:
    _, _, fact_store = _stores()
    state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(),
    ).model_copy(update={"content_hash": "b" * 64})

    with pytest.raises(ValueError, match="content_hash"):
        fact_store.put_state(state)


def test_state_store_rejects_stale_fact_revision_visible_at_as_of() -> None:
    _, official_store, fact_store = _stores()
    first_calendar = official_store.put(
        _record("15-16", observed_at=OBSERVED_AT)
    ).calendar_revision
    assert first_calendar is not None
    first = project_fomc_calendar_fact(first_calendar, policy=FACT_POLICY)
    fact_store.put_fact(first)
    second_at = OBSERVED_AT + timedelta(minutes=1)
    second_calendar = official_store.put(
        _record("16-17", observed_at=second_at)
    ).calendar_revision
    assert second_calendar is not None
    second = project_fomc_calendar_fact(
        second_calendar,
        policy=FACT_POLICY,
        previous=first,
    )
    fact_store.put_fact(second)
    stale_state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=second_at,
        built_at=second_at,
        facts=(first,),
    )

    with pytest.raises(ValueError, match="已过期的事实修订"):
        fact_store.put_state(stale_state)
