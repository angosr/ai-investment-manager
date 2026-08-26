from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from investment_manager.execution.lifecycle.manager import PositionLifecycleManager
from investment_manager.execution.reconciliation.engine import MockReconciler
from investment_manager.governance.models import ReleaseManifest
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.information.official.records import (
    FED_FOMC_CALENDAR_URL,
    FED_SOURCE_ID,
    MarketCalendarEventRevision,
    parse_fomc_calendar,
)
from investment_manager.information.official.repository import SqlStructuredInformationStore
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.information.raw_repository import SqlRawSourcePayloadStore
from investment_manager.information.repository import SqlEventStore
from investment_manager.information.tables import (
    market_calendar_event_revisions,
    source_observations,
)
from investment_manager.legacy.candidate_evaluation import (
    CandidateOutcomeSettler,
    SqlCandidateOutcomeStore,
)
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.exchange import MockExchange
from investment_manager.legacy.repository import (
    SqlFactLedger,
    SqlLifecycleLedger,
    SqlOpenLifecycleRepository,
)
from investment_manager.legacy.tables import (
    account_snapshots,
    candidate_outcomes,
    decision_outcomes,
    metric_observations,
    orders,
)
from investment_manager.market.models import MarketSnapshot, MarketTrade
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine
from investment_manager.portfolio.models import PortfolioAccountSnapshot
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.risk.budget import SqlRiskBudgetStore, portfolio_risk_budgets
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    TriggerNow,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
    build_trigger_plan_patch,
)
from investment_manager.scheduling.repository import (
    PostgresOutboxListener,
    PostgresTriggerLeadership,
    SqlTriggerRepository,
)
from investment_manager.schema import compose_offline_metadata
from investment_manager.state.facts import (
    FOMC_MEETING_FACT_TYPE,
    FactDeltaRule,
    OfficialFactProjectionPolicy,
    StateDeltaPolicy,
    build_state_material_delta,
    build_state_snapshot,
    project_fomc_calendar_fact,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    Materiality,
)
from investment_manager.state.repository import SqlFactStateStore
from investment_manager.state.tables import canonical_fact_revisions

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_postgres_cycle_transaction_and_risk_budget(
    app_config,
    replay_input,
    request: pytest.FixtureRequest,
) -> None:
    database_url = os.environ.get("INVESTMENT_MANAGER_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("未配置隔离的 PostgreSQL 测试数据库")
    if "investment_manager_test" not in database_url:
        raise RuntimeError("集成测试只允许操作名称包含 investment_manager_test 的专用数据库")

    engine = build_engine(database_url)
    request.addfinalizer(engine.dispose)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("该契约测试必须使用 PostgreSQL")
    compose_offline_metadata().drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    migration_config = Config(str(ROOT / "alembic.ini"))
    migration_config.set_main_option("script_location", str(ROOT / "migrations"))
    migration_config.set_main_option("sqlalchemy.url", database_url)
    migration_config.attributes["database_url"] = database_url
    command.upgrade(migration_config, "head")
    portfolio_store = SqlPortfolioStore(engine)
    account_barrier = Barrier(2)

    def project_account(index: int) -> PortfolioAccountSnapshot:
        account_barrier.wait()
        with portfolio_store.account_projection_lock(portfolio_id="lock-test"):
            previous = portfolio_store.latest_account(
                portfolio_id="lock-test",
                as_of=datetime(2026, 8, 20, 11, tzinfo=UTC),
            )
            revision = 0 if previous is None else previous.revision + 1
            account = PortfolioAccountSnapshot(
                snapshot_id=f"postgres-lock-account-{index}",
                cycle_id=f"postgres-lock-cycle-{index}",
                portfolio_id="lock-test",
                revision=revision,
                as_of=datetime(2026, 8, 20, 11, tzinfo=UTC),
                observed_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
                settlement_asset="USDT",
                cash_balance=Decimal("10000"),
                equity=Decimal("10000"),
                equity_high_water=Decimal("10000"),
            )
            portfolio_store.record_account(account)
            return account

    with ThreadPoolExecutor(max_workers=2) as pool:
        accounts = tuple(pool.map(project_account, (1, 2)))
    assert sorted(item.revision for item in accounts) == [0, 1]

    official_store = SqlStructuredInformationStore(engine)
    raw_store = SqlRawSourcePayloadStore(engine)
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)

    def calendar_record(year: int, date_text: str, at: datetime):
        html = f"""
        <h4>{year} FOMC Meetings</h4>
        <div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">{date_text}</div>
        </div>
        """
        content = html.encode("utf-8")
        raw = build_raw_source_payload(
            source_id=FED_SOURCE_ID,
            source_url=FED_FOMC_CALENDAR_URL,
            media_type="text/html",
            observed_at=at,
            content=content,
        )
        raw_store.put(raw, content)
        return parse_fomc_calendar(html, observed_at=at)[0]

    concurrent_records = tuple(
        calendar_record(year, "15-16", observed_at)
        for year in range(2030, 2036)
    )
    barriers = {
        record.observation.source_record_id: Barrier(2) for record in concurrent_records
    }

    def put_concurrently(record):
        barriers[record.observation.source_record_id].wait()
        return official_store.put(record)

    paired_records = tuple(record for record in concurrent_records for _ in range(2))
    with ThreadPoolExecutor(max_workers=len(paired_records)) as pool:
        official_writes = tuple(pool.map(put_concurrently, paired_records))
    assert sum(write.inserted for write in official_writes) == len(concurrent_records)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == len(
            concurrent_records
        )
        assert connection.scalar(
            select(func.count()).select_from(market_calendar_event_revisions)
        ) == len(concurrent_records)

    initial = calendar_record(2036, "15-16", observed_at)
    official_store.put(initial)
    revisions = tuple(
        calendar_record(
            2036,
            date_text,
            observed_at + timedelta(minutes=offset),
        )
        for offset, date_text in ((1, "16-17"), (2, "17-18"))
    )
    revision_barrier = Barrier(2)

    def put_revision(record):
        revision_barrier.wait()
        try:
            return official_store.put(record)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        revision_writes = tuple(pool.map(put_revision, revisions))
    assert sum(write is not None for write in revision_writes) in {1, 2}
    with engine.connect() as connection:
        revision_payloads = connection.execute(
            select(market_calendar_event_revisions.c.payload)
            .where(
                market_calendar_event_revisions.c.source_record_id
                == initial.observation.source_record_id
            )
            .order_by(market_calendar_event_revisions.c.observed_at)
        ).scalars()
        revision_chain = tuple(
            MarketCalendarEventRevision.model_validate(payload)
            for payload in revision_payloads
        )
    assert len(revision_chain) in {2, 3}
    assert all(
        current.previous_revision_id == previous.revision_id
        for previous, current in pairwise(revision_chain)
    )
    fact_store = SqlFactStateStore(engine)
    fact_projection_policy = OfficialFactProjectionPolicy(
        version="postgres-fed-fact-v1",
        affected_assets=("BTC", "ETH"),
    )
    root_fact = project_fomc_calendar_fact(
        revision_chain[0],
        policy=fact_projection_policy,
    )
    fact_store.put_fact(root_fact)
    competing_calendars = []
    for offset, date_text in ((3, "18-19"), (4, "19-20")):
        result = official_store.put(
            calendar_record(
                2036,
                date_text,
                observed_at + timedelta(minutes=offset),
            )
        )
        assert result.calendar_revision is not None
        competing_calendars.append(result.calendar_revision)
    competing_facts = tuple(
        project_fomc_calendar_fact(
            calendar,
            policy=fact_projection_policy,
            previous=root_fact,
        )
        for calendar in competing_calendars
    )
    fact_barrier = Barrier(2)

    def put_fact_revision(fact):
        fact_barrier.wait()
        try:
            return fact_store.put_fact(fact)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        fact_writes = tuple(pool.map(put_fact_revision, competing_facts))
    assert sum(write is not None for write in fact_writes) == 1
    with engine.connect() as connection:
        fact_payloads = connection.execute(
            select(canonical_fact_revisions.c.payload)
            .where(canonical_fact_revisions.c.fact_id == root_fact.fact_id)
            .order_by(canonical_fact_revisions.c.observed_at)
        ).scalars()
        fact_chain = tuple(
            CanonicalFactRevision.model_validate(payload) for payload in fact_payloads
        )
    assert len(fact_chain) == 2
    assert fact_chain[1].previous_revision_id == root_fact.revision_id
    baseline_fact = fact_chain[-1]
    baseline_at = observed_at + timedelta(minutes=4, seconds=30)
    baseline_state = build_state_snapshot(
        projection_version="postgres-state-v1",
        analysis_scope="postgres-portfolio",
        as_of=baseline_at,
        built_at=baseline_at,
        facts=(baseline_fact,),
    )
    fact_store.record_state(state=baseline_state, previous_state_id=None)
    non_material_root = build_state_snapshot(
        projection_version="postgres-state-v1",
        analysis_scope="postgres-non-material",
        as_of=baseline_at,
        built_at=baseline_at,
        facts=(baseline_fact,),
    )
    fact_store.record_state(state=non_material_root, previous_state_id=None)
    competing_non_material_states = tuple(
        build_state_snapshot(
            projection_version="postgres-state-v1",
            analysis_scope="postgres-non-material",
            as_of=baseline_at + timedelta(seconds=offset),
            built_at=baseline_at + timedelta(seconds=offset),
            facts=(baseline_fact,),
        )
        for offset in (1, 2)
    )
    non_material_barrier = Barrier(2)

    def record_non_material_state(state):
        non_material_barrier.wait()
        try:
            return fact_store.record_state(
                state=state,
                previous_state_id=non_material_root.state_id,
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        non_material_writes = tuple(
            pool.map(record_non_material_state, competing_non_material_states)
        )
    assert sum(write is not None for write in non_material_writes) == 1
    transition_facts = []
    previous_fact = baseline_fact
    for offset, date_text in ((5, "20-21"), (6, "21-22")):
        calendar_write = official_store.put(
            calendar_record(
                2036,
                date_text,
                observed_at + timedelta(minutes=offset),
            )
        )
        assert calendar_write.calendar_revision is not None
        fact = project_fomc_calendar_fact(
            calendar_write.calendar_revision,
            policy=fact_projection_policy,
            previous=previous_fact,
        )
        fact_store.put_fact(fact)
        transition_facts.append(fact)
        previous_fact = fact
    delta_policy = StateDeltaPolicy(
        version="postgres-fact-delta-v1",
        validity_seconds=3_600,
        horizons_minutes=(60, 240),
        intelligence_risk_factors=("EXTERNAL_INFORMATION",),
        intelligence_reason_code="INTELLIGENCE_EVENT_INSERTED",
        rules=(
            FactDeltaRule(
                fact_type=FOMC_MEETING_FACT_TYPE,
                materiality=Materiality.NORMAL,
                reason_code="FOMC_SCHEDULE_REVISION",
            ),
        ),
    )
    transition_states = tuple(
        build_state_snapshot(
            projection_version="postgres-state-v1",
            analysis_scope="postgres-portfolio",
            as_of=fact.observed_at,
            built_at=fact.observed_at,
            facts=(fact,),
        )
        for fact in transition_facts
    )
    competing_transitions = tuple(
        (
            state,
            build_state_material_delta(
                previous=baseline_state,
                current=state,
                current_facts=(fact,),
                policy=delta_policy,
            ),
        )
        for state, fact in zip(transition_states, transition_facts, strict=True)
    )
    transition_barrier = Barrier(2)

    def record_transition(candidate):
        state, delta = candidate
        assert delta is not None
        transition_barrier.wait()
        try:
            return fact_store.record_transition(state=state, delta=delta)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        transition_writes = tuple(pool.map(record_transition, competing_transitions))
    assert sum(write is not None for write in transition_writes) == 1
    SqlGovernanceRepository(engine).record_release(
        ReleaseManifest(
            manifest_id="release-bootstrap-v1",
            created_at=datetime(2026, 8, 17, tzinfo=UTC),
            status="CHAMPION",
            code_version="historical-bootstrap-v1",
            component_versions=(("pipeline", "off-pipeline-v1"),),
            constitution_version="constitution-v1",
        )
    )
    first_leader = PostgresTriggerLeadership(
        engine, app_config.trigger.dispatcher_advisory_lock_key
    )
    second_leader = PostgresTriggerLeadership(
        engine, app_config.trigger.dispatcher_advisory_lock_key
    )
    assert first_leader.acquire()
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND state = 'idle in transaction'
                  AND query LIKE 'SELECT pg_try_advisory_lock%'
                """
            )
        ) == 0
    assert not second_leader.acquire()
    first_leader.release()
    assert second_leader.acquire()
    second_leader.release()
    with PostgresOutboxListener(engine) as listener:
        assert SqlEventStore(engine, pipeline_id=app_config.pipeline.version).put(
            replay_input.events[0]
        )
        assert listener.wait(1)
        trigger_plans = SqlTriggerRepository(engine, app_config.trigger)
        initial_plan = trigger_plans.create_plan(
            build_initial_trigger_plan(
                symbol="BTCUSDT",
                pipeline_id=app_config.pipeline.version,
                manifest_id="release-bootstrap-v1",
                updated_at=replay_input.market.as_of,
                heartbeat_seconds=900,
            )
        )
        assert listener.wait(1)
        patch = build_trigger_plan_patch(
            plan=initial_plan,
            submitted_at=replay_input.market.as_of,
            operations=(TriggerNow(request_id="postgres-now-1", reason="PostgreSQL 契约"),),
        )
        revised = trigger_plans.apply_patch(
            patch,
            now=replay_input.market.as_of,
            current_manifest_id="release-bootstrap-v1",
        )
        assert revised.plan.revision == 2
        assert listener.wait(1)
    raw_connection = engine.raw_connection()
    try:
        assert raw_connection.driver_connection.autocommit is False
    finally:
        raw_connection.close()
    admission_repository = SqlTriggerRepository(
        engine,
        app_config.trigger.model_copy(update={"minimum_call_interval_seconds": 15}),
    )

    def admission_batch(symbol: str):
        plan = build_initial_trigger_plan(
            symbol=symbol,
            pipeline_id=app_config.pipeline.version,
            manifest_id="release-bootstrap-v1",
            updated_at=replay_input.market.as_of,
            heartbeat_seconds=None,
        )
        trigger = build_trigger_event(
            trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
            review_reason="PostgreSQL 契约复核",
            symbol=symbol,
            pipeline_id=app_config.pipeline.version,
            occurred_at=replay_input.market.as_of,
            observed_at=replay_input.market.as_of,
            priority=100,
            dedup_key=f"postgres-admission-{symbol}",
        )
        return build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=replay_input.market.as_of,
            deadline=replay_input.market.as_of + timedelta(minutes=5),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = tuple(
            pool.map(
                lambda batch: admission_repository.admit_analysis_call(
                    batch,
                    requested_at=replay_input.market.as_of,
                ),
                (admission_batch("BTCUSDT"), admission_batch("ETHUSDT")),
            )
        )
    assert sum(item.admitted for item in admissions) == 1
    assert sum(item.retry_at is not None for item in admissions) == 1
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )

    first = cycle.run(replay_input)
    replayed = cycle.run(replay_input)

    assert replayed == first
    assert first.order is not None
    assert len(cycle.exchange.orders) == 1

    assert first.position_lifecycle is not None
    assert first.account_after is not None
    candidate = first.candidates[0]
    candidate_evaluation_at = candidate.signal_observed_at + timedelta(
        minutes=candidate.horizon_minutes
    )
    candidate_entry_at = candidate.signal_observed_at + timedelta(seconds=1)
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id="postgres-candidate-entry",
            symbol=candidate.symbol,
            aggregate_trade_id=9_000_000_001,
            event_time=candidate_entry_at,
            observed_at=candidate_entry_at,
            price=candidate.reference_price,
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="postgres-contract",
        )
    )
    SqlMarketDataStore(engine).put_trade(
        MarketTrade(
            trade_id="postgres-candidate-exit",
            symbol=candidate.symbol,
            aggregate_trade_id=9_000_000_002,
            event_time=candidate_evaluation_at,
            observed_at=candidate_evaluation_at,
            price=candidate.reference_price * Decimal("1.01"),
            quantity=Decimal("1"),
            buyer_is_maker=False,
            source="postgres-contract",
        )
    )
    candidate_settlement = CandidateOutcomeSettler(
        store=SqlCandidateOutcomeStore(engine),
        evaluation_version=app_config.outcome_evaluation.version,
        maximum_market_age_seconds=app_config.risk.maximum_market_age_seconds,
        settlement_grace_minutes=app_config.outcome_evaluation.settlement_grace_minutes,
    ).settle(as_of=candidate_evaluation_at)
    assert candidate_settlement.settled == 1
    open_repository = SqlOpenLifecycleRepository(engine)
    assert [item.lifecycle.position_id for item in open_repository.list_open()] == [
        first.position_lifecycle.position_id
    ]
    exit_time = first.position_lifecycle.max_exit_at + timedelta(minutes=1)
    exit_market = MarketSnapshot.model_validate(
        {
            **replay_input.market.model_dump(mode="json"),
            "cycle_id": "cycle-postgres-exit-001",
            "as_of": exit_time,
            "observed_at": exit_time,
            "bid": first.position_lifecycle.entry_price + Decimal("0.99"),
            "ask": first.position_lifecycle.entry_price + Decimal("1.01"),
            "last": first.position_lifecycle.entry_price + Decimal("1"),
        }
    )
    lifecycle_ledger = SqlLifecycleLedger(engine)
    manager = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
        lifecycle_ledger=lifecycle_ledger,
    )

    closed = manager.evaluate(
        lifecycle=first.position_lifecycle,
        market=exit_market,
        account=first.account_after,
        pipeline_version=app_config.pipeline.version,
    )
    replayed_close = manager.evaluate(
        lifecycle=first.position_lifecycle,
        market=exit_market,
        account=first.account_after,
        pipeline_version=app_config.pipeline.version,
    )

    assert closed == replayed_close
    assert closed.outcome is not None
    assert open_repository.list_open() == ()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(orders)) == 2
        assert connection.scalar(select(func.count()).select_from(account_snapshots)) == 3
        assert connection.scalar(select(func.count()).select_from(decision_outcomes)) == 1
        assert connection.scalar(select(func.count()).select_from(candidate_outcomes)) == 1
        assert connection.scalar(select(func.count()).select_from(metric_observations)) == 17
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
        assert budget["reserved_amount"] == 0
        assert budget["exposure_risk_amount"] == 0
