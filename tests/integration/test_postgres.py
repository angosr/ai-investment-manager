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

from quant_core.asset_management import CanonicalFactRevision
from quant_core.candidate_evaluation import CandidateOutcomeSettler, SqlCandidateOutcomeStore
from quant_core.cycle import AnalysisCycle
from quant_core.domain import MarketSnapshot
from quant_core.execution import MockExchange
from quant_core.fact_pipeline import (
    OfficialFactProjectionPolicy,
    project_fomc_calendar_fact,
)
from quant_core.fact_state_sql import SqlFactStateStore
from quant_core.governance import ReleaseManifest
from quant_core.governance_context import GovernanceSnapshotAssembler
from quant_core.lifecycle import PositionLifecycleManager
from quant_core.market_data import MarketTrade
from quant_core.market_data_sql import SqlMarketDataStore, market_metadata
from quant_core.official_information import MarketCalendarEventRevision, parse_fomc_calendar
from quant_core.official_information_sql import SqlOfficialInformationStore
from quant_core.persistence import (
    SqlEventStore,
    SqlFactLedger,
    SqlGovernanceRepository,
    SqlLifecycleLedger,
    SqlOpenLifecycleRepository,
    SqlRiskBudgetStore,
    account_snapshots,
    build_engine,
    candidate_outcomes,
    canonical_fact_revisions,
    decision_outcomes,
    market_calendar_event_revisions,
    metadata,
    metric_observations,
    orders,
    portfolio_risk_budgets,
    source_observations,
)
from quant_core.reconciliation import MockReconciler
from quant_core.trigger import (
    AnalysisTriggerType,
    TriggerNow,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
    build_trigger_plan_patch,
)
from quant_core.trigger_sql import (
    PostgresOutboxListener,
    PostgresTriggerLeadership,
    SqlTriggerRepository,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_postgres_cycle_transaction_and_risk_budget(app_config, replay_input) -> None:
    database_url = os.environ.get("QUANT_CORE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("未配置隔离的 PostgreSQL 测试数据库")
    if "quant_core_test" not in database_url:
        raise RuntimeError("集成测试只允许操作名称包含 quant_core_test 的专用数据库")

    engine = build_engine(database_url)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("该契约测试必须使用 PostgreSQL")
    market_metadata.drop_all(engine)
    metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    migration_config = Config(str(ROOT / "alembic.ini"))
    migration_config.set_main_option("script_location", str(ROOT / "migrations"))
    migration_config.set_main_option("sqlalchemy.url", database_url)
    migration_config.attributes["database_url"] = database_url
    command.upgrade(migration_config, "head")
    official_store = SqlOfficialInformationStore(engine)
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    concurrent_records = tuple(
        parse_fomc_calendar(
            f"""
            <h4>{year} FOMC Meetings</h4>
            <div class="row fomc-meeting">
              <div class="fomc-meeting__month"><strong>September</strong></div>
              <div class="fomc-meeting__date">15-16</div>
            </div>
            """,
            observed_at=observed_at,
        )[0]
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

    initial = parse_fomc_calendar(
        """
        <h4>2036 FOMC Meetings</h4>
        <div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">15-16</div>
        </div>
        """,
        observed_at=observed_at,
    )[0]
    official_store.put(initial)
    revisions = tuple(
        parse_fomc_calendar(
            f"""
            <h4>2036 FOMC Meetings</h4>
            <div class="row fomc-meeting">
              <div class="fomc-meeting__month"><strong>September</strong></div>
              <div class="fomc-meeting__date">{date_text}</div>
            </div>
            """,
            observed_at=observed_at + timedelta(minutes=offset),
        )[0]
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
    root_fact = project_fomc_calendar_fact(
        revision_chain[0],
        policy=OfficialFactProjectionPolicy(
            version="postgres-fed-fact-v1",
            affected_assets=("BTC", "ETH"),
        ),
    )
    fact_store.put_fact(root_fact)
    competing_calendars = []
    for offset, date_text in ((3, "18-19"), (4, "19-20")):
        result = official_store.put(
            parse_fomc_calendar(
                f"""
                <h4>2036 FOMC Meetings</h4>
                <div class="row fomc-meeting">
                  <div class="fomc-meeting__month"><strong>September</strong></div>
                  <div class="fomc-meeting__date">{date_text}</div>
                </div>
                """,
                observed_at=observed_at + timedelta(minutes=offset),
            )[0]
        )
        assert result.calendar_revision is not None
        competing_calendars.append(result.calendar_revision)
    competing_facts = tuple(
        project_fomc_calendar_fact(
            calendar,
            policy=OfficialFactProjectionPolicy(
                version="postgres-fed-fact-v1",
                affected_assets=("BTC", "ETH"),
            ),
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
    governance_metrics = dict(
        GovernanceSnapshotAssembler(engine, app_config, project_root=ROOT)
        .build(as_of=candidate_evaluation_at)
        .metric_summaries
    )
    evidence_prefix = ":".join(
        (
            "candidate_evidence",
            candidate.producer_id,
            candidate.producer_version,
            candidate.symbol,
            candidate.side.value,
            str(candidate.horizon_minutes),
            candidate.calibration_ref,
            app_config.outcome_evaluation.version,
            app_config.execution.version,
            app_config.frequency.version,
        )
    )
    assert governance_metrics[f"{evidence_prefix}:SETTLED:count"] == "1"
    assert governance_metrics[f"{evidence_prefix}:non_overlapping_settled"] == "1"
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
