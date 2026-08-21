"""集成测试：跑真实回放周期落库，再经观测台读取层+投影层还原为 DTO。

用真实持久化的 payload（而非替身）验证 read_models 与 serializers，覆盖字段名一致性、
周期轨推断与信息快照投影。SQLite 内存库，无外部依赖。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import create_engine, insert

from investment_manager.entrypoints.dashboard import serializers as ser
from investment_manager.entrypoints.dashboard.app import create_app
from investment_manager.entrypoints.dashboard.read_models import DashboardReader
from investment_manager.execution.models import ExitReason
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.models import (
    AssessmentUncertainty,
    ContextAssessment,
    ContextView,
    PricedState,
)
from investment_manager.forecast.tables import codex_runs, context_assessments
from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.repository import SqlEventStore
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.exchange import MockExchange
from investment_manager.legacy.models import DecisionOutcome, DirectionalView
from investment_manager.legacy.repository import (
    SqlFactLedger,
    decision_outcomes,
)
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.scheduling.models import AnalysisTriggerType, build_trigger_event
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.scheduling.tables import analysis_call_admissions
from investment_manager.schema import create_schema
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDelta,
    PacketPortfolioState,
    RequiredView,
)
from investment_manager.state.tables import decision_packets


def _seed_cycle(app_config, replay_input, *, database_url: str | None = None):
    engine = create_engine(database_url or "sqlite+pysqlite:///:memory:")
    create_schema(engine)
    ledger = SqlFactLedger(engine)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    result = cycle.run(replay_input)
    return engine, result


def _dashboard_assessment_packet(*, as_of: datetime, analysis_scope: str) -> DecisionPacket:
    return DecisionPacket.create(
        schema_version="decision-packet-v4",
        policy_version="dashboard-packet-v1",
        mandate_version="dashboard-test-mandate-v1",
        analysis_scope=analysis_scope,
        as_of=as_of,
        state_id="dashboard-state-1",
        question="评估当前组合风险与方向倾向。",
        trigger_ids=("dashboard-delta-1",),
        required_views=(RequiredView(asset="BTC", horizon_minutes=60),),
        portfolio=PacketPortfolioState(
            quote_balance=Decimal("10000"),
            equity=Decimal("10000"),
            daily_pnl=Decimal("0"),
            drawdown_fraction=Decimal("0"),
            open_order_count=0,
            kill_switch_active=False,
            reconciled=True,
            positions=(),
        ),
        asset_states=(
            PacketAssetState(
                asset="BTC",
                market_symbol="BTCUSDT",
                observed_at=as_of,
                bid=Decimal("59999"),
                ask=Decimal("60001"),
                last=Decimal("60000"),
                return_fraction=Decimal("0.01"),
                realized_volatility=Decimal("0.30"),
                atr=Decimal("900"),
                spread_bps=Decimal("0.33"),
                volume_ratio=Decimal("1.20"),
                regime="TRENDING_UP",
                market_age_seconds=0,
            ),
        ),
        deltas=(
            PacketDelta(
                delta_id="dashboard-delta-1",
                policy_version="dashboard-delta-v1",
                category="MARKET",
                materiality="HIGH",
                observed_at=as_of,
                expires_at=as_of + timedelta(hours=1),
                affected_assets=("BTC",),
                risk_factors=("CRYPTO_BETA",),
                horizons_minutes=(60,),
                fact_revision_ids=(),
                feature_snapshot_refs=("dashboard-feature-1",),
                reason_codes=("MARKET_REGIME_CHANGED",),
            ),
        ),
        facts=(),
        active_hypotheses=(),
        previous_assessment_refs=(),
        data_quality_codes=(),
        coverage_gap_codes=(),
        missing_fact_revision_ids=(),
        omitted_fact_revision_ids=(),
    )


def test_capital_dashboard_keeps_assessment_history_in_a_separate_read_only_store(
    app_config,
    replay_input,
    tmp_path,
) -> None:
    primary_url = f"sqlite+pysqlite:///{tmp_path / 'capital.db'}"
    assessment_url = f"sqlite+pysqlite:///{tmp_path / 'assessment.db'}"
    primary_engine = create_engine(primary_url)
    create_schema(primary_engine)
    archive_engine, result = _seed_cycle(
        app_config,
        replay_input,
        database_url=assessment_url,
    )
    as_of = datetime(2026, 8, 18, 12, tzinfo=UTC)
    packet = _dashboard_assessment_packet(
        as_of=as_of,
        analysis_scope="primary-portfolio",
    )
    assessment = ContextAssessment(
        assessment_id="context-assessment-dashboard",
        analysis_scope="primary-portfolio",
        mandate_version="dashboard-test-mandate-v1",
        as_of=as_of,
        available_at=datetime(2026, 8, 18, 12, 0, 10, tzinfo=UTC),
        analysis_behavior_hash="b" * 64,
        decision_packet_hash=packet.content_hash,
        trigger_ids=packet.trigger_ids,
        market_mechanism="宏观事实尚不足以形成可靠的短期方向判断。",
        views=(
            ContextView(
                asset="BTC",
                horizon_minutes=60,
                direction=DirectionalView.UNCERTAIN,
                already_priced=PricedState.UNKNOWN,
                uncertainty=AssessmentUncertainty.HIGH,
                invalidation_conditions=("出现新的可靠方向证据",),
            ),
        ),
        data_gaps=("缺少可靠方向证据",),
    )
    with archive_engine.begin() as connection:
        connection.execute(
            insert(decision_packets).values(
                packet_id=packet.packet_id,
                analysis_scope=assessment.analysis_scope,
                as_of=assessment.as_of,
                policy_version=packet.policy_version,
                content_hash=assessment.decision_packet_hash,
                payload=packet.model_dump(mode="json"),
            )
        )
        connection.execute(
            insert(context_assessments).values(
                assessment_id=assessment.assessment_id,
                packet_id=packet.packet_id,
                analysis_scope=assessment.analysis_scope,
                available_at=assessment.available_at,
                analysis_behavior_hash=assessment.analysis_behavior_hash,
                view_count=len(assessment.views),
                payload=assessment.model_dump(mode="json"),
            )
        )
    SqlEventStore(
        archive_engine,
        pipeline_id=app_config.pipeline.version,
    ).put(
        IntelligenceEvent(
            evidence_id="dashboard-assessment-news",
            event_time=assessment.as_of,
            observed_at=assessment.available_at,
            source="official-calendar",
            title="Assessment 库中的一手事件",
            body="用于确认 Capital 观测台读取正确的情报事实库。",
            symbols=("BTCUSDT",),
            relevance=Decimal("0.8"),
            impact=Decimal("0.7"),
            source_reliability=Decimal("0.9"),
            novelty=Decimal("0.8"),
        )
    )

    application = create_app(
        app_config,
        primary_url,
        assessment_database_url=assessment_url,
    )

    async def read_endpoints():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            return await asyncio.gather(
                client.get("/api/assessment/cycles"),
                client.get(f"/api/assessment/cycles/{result.cycle_id}"),
                client.get("/api/assessment/records"),
                client.get(f"/api/assessment/records/{assessment.assessment_id}"),
                client.get("/api/capital/activity"),
                client.get("/api/events"),
            )

    rows, detail, assessment_rows, assessment_detail, capital_rows, events = asyncio.run(
        read_endpoints()
    )

    assert rows.status_code == 200
    assert [item["cycle_id"] for item in rows.json()["cycles"]] == [result.cycle_id]
    assert detail.status_code == 200
    assert detail.json()["cycle_id"] == result.cycle_id
    assert assessment_rows.status_code == 200
    assert assessment_rows.json()["assessments"][0]["assessment_id"] == (assessment.assessment_id)
    assert assessment_detail.status_code == 200
    assert assessment_detail.json()["views"][0]["direction"] == "UNCERTAIN"
    assert assessment_detail.json()["views"][0]["outcome"] is None
    assert assessment_detail.json()["input_snapshot"]["packet_id"] == packet.packet_id
    assert assessment_detail.json()["input_snapshot"]["state_id"] == packet.state_id
    assert capital_rows.status_code == 200
    assert capital_rows.json() == {"actions": []}
    assert events.status_code == 200
    assert any(
        item["title"] == "Assessment 库中的一手事件"
        for item in events.json()["events"]
    )


def test_reader_and_serializer_render_a_real_persisted_cycle(app_config, replay_input) -> None:
    engine, result = _seed_cycle(app_config, replay_input)
    reader = DashboardReader(engine, app_config)

    rows = reader.list_cycles(before=None, limit=10)
    assert len(rows) == 1
    row_dto = ser.cycle_row(rows[0])
    assert row_dto["cycle_id"] == result.cycle_id
    assert row_dto["symbol"]  # market_snapshots join filled the symbol
    assert row_dto["summary"]  # 一句人话摘要非空
    assert row_dto["category"] in {"exec", "pending", "rejected", "no-trade", "no-action"}


def test_cycle_detail_and_snapshot_project_real_payloads(app_config, replay_input) -> None:
    engine, result = _seed_cycle(app_config, replay_input)
    reader = DashboardReader(engine, app_config)

    facts = reader.get_cycle(result.cycle_id)
    assert facts is not None
    detail = ser.cycle_detail(facts)

    # 周期轨语义由后端一次性给出，前端不再重复推断。
    gates = detail["rail"]["gates"]
    assert len(gates) == 9
    assert gates[0] == {
        "key": "panel",
        "label": "面板就绪",
        "state": "pass",
        "note": "",
    }

    # 信息快照：必读层三块都要有真实字段
    snapshot = detail["snapshot"]
    assert snapshot["symbol"]
    assert snapshot["market"]["last"] is not None
    assert snapshot["features"]["regime"] in {
        "TRENDING_UP",
        "TRENDING_DOWN",
        "RANGING",
        "UNKNOWN",
    }
    assert "reconciled" in snapshot["account"]
    assert isinstance(snapshot["evidence"], list)
    assert len(snapshot["rules"]) >= 1  # 固定规则一定被注入


def test_cycle_rail_stops_at_uncalibrated_candidate_gate(base_app_config, replay_input) -> None:
    engine, result = _seed_cycle(base_app_config, replay_input)
    facts = DashboardReader(engine, base_app_config).get_cycle(result.cycle_id)
    assert facts is not None

    gates = ser.cycle_detail(facts)["rail"]["gates"]
    states = {gate["key"]: gate["state"] for gate in gates}

    assert states["candidate"] == "pass"
    assert states["intent"] == "stop"
    assert states["frequency"] == "skip"
    assert states["risk"] == "skip"
    assert states["execution"] == "skip"


def test_equity_and_health_run_against_real_engine(app_config, replay_input) -> None:
    engine, _ = _seed_cycle(app_config, replay_input)
    reader = DashboardReader(engine, app_config)
    now = datetime.now(UTC) + timedelta(hours=1)

    equity = ser.equity(reader.equity_window(now=now, hours=48))
    assert equity["trade_count"] >= 0  # 回放未平仓则为 0，结构仍合法

    accounts = [ser.account_status(status) for status in reader.accounts(now=now)]
    assert len(accounts) == 3
    assert all(account["state"] == "DISABLED" for account in accounts)


def test_assessment_health_reads_the_current_context_chain(base_app_config) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    config = base_app_config.model_copy(
        update={
            "assessment": base_app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": base_app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    now = datetime(2026, 8, 21, 9, tzinfo=UTC)
    completed_at = now - timedelta(minutes=5)
    packet_id = "packet-dashboard-assessment"
    behavior_hash = configured_assess_behavior_hash(config)
    with engine.begin() as connection:
        connection.execute(
            insert(decision_packets).values(
                packet_id=packet_id,
                analysis_scope=config.assessment.mandate.analysis_scope,
                as_of=now - timedelta(minutes=10),
                policy_version=config.decision_state.packet_policy.version,
                content_hash="a" * 64,
                payload={},
            )
        )
        connection.execute(
            insert(codex_runs),
            (
                {
                    "run_id": "run-dashboard-current",
                    "cycle_id": packet_id,
                    "account_id": ".codex-test",
                    "attempt": 0,
                    "status": "SUCCEEDED",
                    "error_class": None,
                    "payload": {
                        "analysis_behavior_hash": behavior_hash,
                        "completed_at": completed_at.isoformat(),
                    },
                },
                {
                    "run_id": "run-dashboard-retired",
                    "cycle_id": packet_id,
                    "account_id": ".codex-retired",
                    "attempt": 0,
                    "status": "FAILED",
                    "error_class": "RETIRED_BEHAVIOR",
                    "payload": {"analysis_behavior_hash": "b" * 64},
                },
            ),
        )
        connection.execute(
            insert(context_assessments).values(
                assessment_id="assessment-dashboard-current",
                packet_id=packet_id,
                analysis_scope=config.assessment.mandate.analysis_scope,
                available_at=completed_at,
                analysis_behavior_hash=behavior_hash,
                view_count=sum(
                    len(asset.horizons_minutes) for asset in config.assessment.mandate.assets
                ),
                payload={},
            )
        )

    status = DashboardReader(engine, config).analysis_runtime_status(now=now)

    assert status.recent_attempts == 1
    assert status.recent_successes == 1
    assert status.overdue_forecast_count == 0
    assert {scope.latest_success_at for scope in status.scopes} == {completed_at}


def test_equity_is_account_wide_and_uses_actual_close_time(app_config, replay_input) -> None:
    engine, _ = _seed_cycle(app_config, replay_input)
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)

    def outcome(index: int, *, pipeline: str, closed_at: datetime) -> DecisionOutcome:
        opened_at = closed_at - timedelta(hours=1)
        return DecisionOutcome(
            outcome_id=f"portfolio-outcome-{index}",
            cycle_id=f"portfolio-cycle-{index}",
            intent_id=f"portfolio-intent-{index}",
            pipeline_version=pipeline,
            position_id=f"portfolio-position-{index}",
            symbol="BTCUSDT",
            opened_at=opened_at,
            closed_at=closed_at,
            exit_reason=ExitReason.MAX_HOLDING_TIME,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            gross_pnl=Decimal("1"),
            total_fees=Decimal("0.2"),
            net_pnl=Decimal("0.8"),
            maximum_favorable_excursion=Decimal("1"),
            maximum_adverse_excursion=Decimal("-0.5"),
        )

    included = (
        outcome(1, pipeline="retired-pipeline-v1", closed_at=now - timedelta(hours=2)),
        outcome(2, pipeline=app_config.pipeline.version, closed_at=now - timedelta(hours=1)),
    )
    excluded = outcome(
        3,
        pipeline="retired-pipeline-v1",
        closed_at=now - timedelta(hours=49),
    )
    with engine.begin() as connection:
        for item in (*included, excluded):
            connection.execute(
                insert(decision_outcomes).values(
                    outcome_id=item.outcome_id,
                    cycle_id=item.cycle_id,
                    intent_id=item.intent_id,
                    position_id=item.position_id,
                    net_pnl=item.net_pnl,
                    payload=item.model_dump(mode="json"),
                )
            )

    result = ser.equity(DashboardReader(engine, app_config).equity_window(now=now, hours=48))

    assert result["trade_count"] == 2
    assert result["summary"]["net_pnl"] == "1.6"
    assert result["summary"]["total_fees"] == "0.4"


def test_dashboard_call_activity_reads_global_admissions(app_config, replay_input) -> None:
    engine, _ = _seed_cycle(app_config, replay_input)
    now = replay_input.market.as_of
    with engine.begin() as connection:
        for index, admitted_at in enumerate(
            (now - timedelta(minutes=59), now, now + timedelta(seconds=1)),
            start=1,
        ):
            connection.execute(
                insert(analysis_call_admissions).values(
                    batch_id=f"batch-{index}",
                    pipeline_id="pipeline-v1",
                    symbol="BTCUSDT",
                    admitted_at=admitted_at,
                    payload={"batch_id": f"batch-{index}"},
                )
            )

    assert DashboardReader(engine, app_config).ai_calls_last_hour(now=now) == 2


def test_open_position_is_read_and_serialized(app_config, replay_input) -> None:
    engine, _result = _seed_cycle(app_config, replay_input)
    reader = DashboardReader(engine, app_config)

    records = reader.open_positions()
    assert len(records) == 1  # 回放为 EXECUTED，留下一个未平仓生命周期
    marks = reader.latest_prices()
    sides = reader.entry_sides([records[0].lifecycle.cycle_id])
    dto = ser.position(
        records[0],
        mark=marks.get(records[0].lifecycle.symbol),
        side=sides.get(records[0].lifecycle.cycle_id),
    )
    assert dto["symbol"]
    assert dto["direction"] == "多"  # 建仓订单方向 BUY → 多头
    assert dto["entry_price"] is not None
    assert dto["stop_price"] is not None
    assert dto["status"] in {"PROTECTION_PENDING", "PROTECTED", "PROTECTION_FAILED"}


def test_news_fed_into_a_cycle_links_back_to_it(app_config, replay_input) -> None:
    engine, result = _seed_cycle(app_config, replay_input)
    facts = SqlFactLedger(engine).get(result.cycle_id)
    assert facts is not None and facts.panel.evidence  # 回放面板选入了证据
    evidence_id = facts.panel.evidence[0].evidence_id

    # 补一条与该证据同 id 的采集事件，模拟这条新闻确实被喂进了那次分析
    now = facts.panel.as_of
    SqlEventStore(engine, pipeline_id=app_config.pipeline.version).put(
        IntelligenceEvent(
            evidence_id=evidence_id,
            event_time=now,
            observed_at=now,
            source="金十",
            title="现货 ETF 净流入转正",
            body="多只现货 ETF 合计净流入，情绪转暖。",
            symbols=("BTCUSDT",),
            relevance=Decimal("0.8"),
            impact=Decimal("0.7"),
            source_reliability=Decimal("0.9"),
            novelty=Decimal("0.6"),
        )
    )

    reader = DashboardReader(engine, app_config)
    events = [ser.world_event(event) for event in reader.list_events(before=None, limit=20)]
    fed = next(event for event in events if event["kind"] == "NEWS")
    assert fed["fed_cycle_id"] == result.cycle_id
    assert fed["fed_cycle_at"] is not None


def test_agent_wakeup_is_attributed_to_main_agent(app_config, replay_input) -> None:
    engine, _ = _seed_cycle(app_config, replay_input)
    now = replay_input.market.as_of
    SqlTriggerRepository(engine, app_config.trigger).record_trigger(
        build_trigger_event(
            trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
            review_reason="Dashboard 立即复核",
            symbol="BTCUSDT",
            pipeline_id=app_config.pipeline.version,
            occurred_at=now,
            observed_at=now,
            priority=100,
            dedup_key="dashboard-agent-wakeup",
        )
    )

    event = next(
        item
        for item in DashboardReader(engine, app_config).list_events(before=None, limit=20)
        if item.kind == "AGENT_WAKEUP"
    )

    assert event.source == "主 Agent"
    assert event.title == "Agent 立即复核（BTCUSDT）"
