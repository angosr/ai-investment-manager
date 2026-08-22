"""集成测试：跑真实回放周期落库，再经观测台读取层+投影层还原为 DTO。

用真实持久化的 payload（而非替身）验证 read_models 与 serializers，覆盖字段名一致性、
周期轨推断与信息快照投影。SQLite 内存库，无外部依赖。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import create_engine, func, insert, select

from investment_manager.entrypoints.dashboard import serializers as ser
from investment_manager.entrypoints.dashboard.app import create_app
from investment_manager.entrypoints.dashboard.read_models import AssessmentRecord, DashboardReader
from investment_manager.execution.models import ExitReason
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.executor import (
    AssessmentExecution,
    AssessmentExecutionStatus,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.models import (
    AssessmentUncertainty,
    ContextAssessment,
    ContextAssessmentSchemaVersion,
    ContextCapitalEffect,
    ContextCapitalImplication,
    ContextCausalNode,
    ContextDecisionBlocker,
    ContextDriver,
    ContextDriverStatus,
    ContextEventImpactState,
    ContextEventReference,
    ContextHypothesis,
    ContextHypothesisRole,
    ContextView,
    PricedState,
)
from investment_manager.forecast.policy import CodexAccount, CodexAccountRegistry
from investment_manager.forecast.tables import codex_runs, context_assessments
from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.repository import SqlEventStore
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.exchange import MockExchange
from investment_manager.legacy.models import DecisionOutcome, DirectionalView
from investment_manager.legacy.repository import (
    SqlFactLedger,
    analysis_cycles,
    decision_outcomes,
    market_snapshots,
)
from investment_manager.platform.fact_store import (
    FactStoreRole,
    SqlFactCohortQuarantineStore,
    build_fact_cohort_quarantine,
    require_fact_store_role,
)
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.scheduling.models import AnalysisTriggerType, build_trigger_event
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.scheduling.tables import (
    analysis_call_admissions,
    analysis_trigger_events,
)
from investment_manager.schema import create_schema
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDelta,
    PacketIntelligenceEvent,
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
        intelligence_events=(
            PacketIntelligenceEvent(
                evidence_ref="d" * 64,
                evidence_id="dashboard-world-event",
                normalizer_version="dashboard-test-v1",
                acquisition_route="dashboard-test",
                source="official-calendar",
                event_time=as_of - timedelta(minutes=5),
                observed_at=as_of - timedelta(minutes=4),
                title="重要政策日程发生变化",
                body="该事件用于验证世界认知引用与输入快照来自同一冻结 Packet。",
                symbols=("BTCUSDT",),
                relevance=Decimal("0.9"),
                impact=Decimal("0.8"),
                source_reliability=Decimal("0.9"),
                novelty=Decimal("0.8"),
                directly_triggered=False,
            ),
        ),
        active_hypotheses=(),
        previous_assessment_refs=(),
        data_quality_codes=(),
        coverage_gap_codes=(),
        missing_fact_revision_ids=(),
        omitted_fact_revision_ids=(),
    )


def test_world_model_assessment_dto_has_one_traceable_contract() -> None:
    as_of = datetime(2026, 8, 22, 18, tzinfo=UTC)
    packet = _dashboard_assessment_packet(as_of=as_of, analysis_scope="primary-portfolio")
    evidence_id = "d" * 64
    assessment = ContextAssessment(
        schema_version=ContextAssessmentSchemaVersion.WORLD_MODEL_V1,
        assessment_id="world-model-dashboard",
        analysis_scope="primary-portfolio",
        mandate_version="dashboard-test-mandate-v1",
        as_of=as_of,
        available_at=as_of + timedelta(seconds=10),
        analysis_behavior_hash="a" * 64,
        decision_packet_hash=packet.content_hash,
        trigger_ids=packet.trigger_ids,
        hypotheses=(
            ContextHypothesis(
                hypothesis_id="hypothesis-dashboard-primary",
                role=ContextHypothesisRole.PRIMARY,
                claim="政策预期仍是当前风险偏好变化的主要可检验解释。",
                horizon_hours=72,
                causal_chain=(
                    ContextCausalNode(
                        statement="官方政策日程已经发生变化。",
                        evidence_ids=(evidence_id,),
                    ),
                    ContextCausalNode(
                        statement="市场风险偏好可能在正式结果前重新定价。",
                        evidence_ids=(evidence_id,),
                    ),
                ),
                next_observation="观察正式政策结果及同步跨资产响应。",
                invalidation_conditions=("官方取消日程且风险资产未发生同步响应",),
                next_review_at=as_of + timedelta(hours=6),
            ),
        ),
        capital_implication=ContextCapitalImplication(
            objective_id="carry-program-base",
            effect=ContextCapitalEffect.CAUTION,
            incremental_reason="政策结果可能增加程序基线未覆盖的事件风险。",
            transmission="政策结果经风险偏好与双腿流动性影响下一次 carry 入场。",
            evidence_ids=(evidence_id,),
            invalidation_conditions=("政策落地后双腿流动性与基差保持稳定",),
        ),
        decision_blockers=(
            ContextDecisionBlocker(
                question="政策结果是否造成双腿流动性同步恶化？",
                action_if_yes="保留入场反对候选供配对评价。",
                action_if_no="维持程序基线。",
                observation_needed="正式结果后的现货深度、永续深度与基差响应。",
            ),
        ),
    )

    dto = ser.assessment_detail(AssessmentRecord(assessment=assessment, packet=packet))

    assert dto["schema_version"] == "world-model-assessment-v1"
    assert dto["mechanism"] == assessment.hypotheses[0].claim
    assert dto["drivers"] == []
    assert dto["views"] == []
    assert dto["data_gaps"] == []
    assert (
        dto["hypotheses"][0]["causal_chain"][0]["evidence"][0]["evidence_id"]
        == evidence_id
    )
    assert dto["capital_implication"]["effect"] == "CAUTION"
    assert dto["capital_implication"]["capital_authority"] == "NONE"
    assert dto["decision_blockers"][0]["question"].startswith("政策结果")
    assert (
        dto["cited_evidence"]
        == dto["hypotheses"][0]["causal_chain"][0]["evidence"]
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
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    packet = _dashboard_assessment_packet(
        as_of=as_of,
        analysis_scope="primary-portfolio",
    )
    assessment = ContextAssessment(
        assessment_id="context-assessment-dashboard",
        analysis_scope="primary-portfolio",
        mandate_version="dashboard-test-mandate-v1",
        as_of=as_of,
        available_at=as_of + timedelta(seconds=10),
        analysis_behavior_hash=configured_assess_behavior_hash(app_config),
        decision_packet_hash=packet.content_hash,
        trigger_ids=packet.trigger_ids,
        market_mechanism="宏观事实尚不足以形成可靠的短期方向判断。",
        drivers=(
            ContextDriver(
                statement="政策日程仍可能改变未来风险溢价。",
                status=ContextDriverStatus.INFERRED,
                transmission="政策预期先影响风险偏好，再影响现货需求与价格。",
                evidence_ids=("d" * 64,),
                invalidation_conditions=("官方取消日程或市场完成定价",),
            ),
        ),
        event_references=(
            ContextEventReference(
                evidence_id="d" * 64,
                source="official-calendar",
                title="重要政策日程发生变化",
                event_time=as_of - timedelta(minutes=5),
                impact_state=ContextEventImpactState.ACTIVE,
                rationale="正式日程尚未结束，未来政策预期仍可能变化。",
            ),
        ),
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
    bad_packet = _dashboard_assessment_packet(
        as_of=as_of + timedelta(seconds=1),
        analysis_scope="primary-portfolio",
    )
    bad_assessment = ContextAssessment(
        assessment_id="context-assessment-dashboard-invalid-language",
        analysis_scope="primary-portfolio",
        mandate_version="dashboard-test-mandate-v1",
        as_of=bad_packet.as_of,
        available_at=as_of + timedelta(seconds=20),
        analysis_behavior_hash="c" * 64,
        decision_packet_hash=bad_packet.content_hash,
        trigger_ids=bad_packet.trigger_ids,
        market_mechanism=(
            "Accepted evidence suggests rising ETH while market_mechanism希望错误"
        ),
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
        data_gaps=("Portfolio equity is unavailable.], views错误",),
    )
    with archive_engine.begin() as connection:
        connection.execute(
            insert(decision_packets),
            (
                {
                    "packet_id": packet.packet_id,
                    "analysis_scope": assessment.analysis_scope,
                    "as_of": assessment.as_of,
                    "policy_version": packet.policy_version,
                    "content_hash": assessment.decision_packet_hash,
                    "payload": packet.model_dump(mode="json"),
                },
                {
                    "packet_id": bad_packet.packet_id,
                    "analysis_scope": bad_assessment.analysis_scope,
                    "as_of": bad_assessment.as_of,
                    "policy_version": bad_packet.policy_version,
                    "content_hash": bad_assessment.decision_packet_hash,
                    "payload": bad_packet.model_dump(mode="json"),
                },
            ),
        )
        connection.execute(
            insert(context_assessments),
            (
                {
                    "assessment_id": assessment.assessment_id,
                    "packet_id": packet.packet_id,
                    "analysis_scope": assessment.analysis_scope,
                    "available_at": assessment.available_at,
                    "analysis_behavior_hash": assessment.analysis_behavior_hash,
                    "view_count": len(assessment.views),
                    "payload": assessment.model_dump(mode="json"),
                },
                {
                    "assessment_id": bad_assessment.assessment_id,
                    "packet_id": bad_packet.packet_id,
                    "analysis_scope": bad_assessment.analysis_scope,
                    "available_at": bad_assessment.available_at,
                    "analysis_behavior_hash": bad_assessment.analysis_behavior_hash,
                    "view_count": len(bad_assessment.views),
                    "payload": bad_assessment.model_dump(mode="json"),
                },
            ),
        )
        connection.execute(
            insert(codex_runs).values(
                run_id="run-dashboard-invalid-output",
                cycle_id=bad_packet.packet_id,
                account_id=".codex-test",
                attempt=1,
                status="FAILED",
                error_class="SCHEMA_INVALID",
                payload={
                    "analysis_behavior_hash": configured_assess_behavior_hash(
                        app_config
                    ),
                    "completed_at": bad_assessment.available_at.isoformat(),
                },
            )
        )
    SqlContextAssessmentStore(archive_engine).record_execution(
        AssessmentExecution.create(
            status=AssessmentExecutionStatus.FAILED,
            packet_id=bad_packet.packet_id,
            analysis_behavior_hash=configured_assess_behavior_hash(app_config),
            completed_at=bad_assessment.available_at,
            codex_attempts=1,
            reason_code="CODEX_SCHEMA_INVALID",
            source_run_id="run-dashboard-invalid-output",
            account_id=".codex-test",
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
        assessment_config=app_config,
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
                client.get(
                    f"/api/assessment/records/{bad_assessment.assessment_id}"
                ),
                client.get("/api/capital/activity"),
                client.get("/api/events"),
            )

    (
        rows,
        detail,
        assessment_rows,
        assessment_detail,
        bad_assessment_detail,
        capital_rows,
        events,
    ) = asyncio.run(read_endpoints())

    assert rows.status_code == 200
    assert [item["cycle_id"] for item in rows.json()["cycles"]] == [result.cycle_id]
    assert detail.status_code == 200
    assert detail.json()["cycle_id"] == result.cycle_id
    assert assessment_rows.status_code == 200
    assert [
        item["assessment_id"]
        for item in assessment_rows.json()["assessments"][:2]
    ] == [bad_assessment.assessment_id, assessment.assessment_id]
    assert assessment_rows.json()["quality"] == {
        "latest_attempt_at": bad_assessment.available_at.isoformat(),
        "latest_attempt_status": "REJECTED",
        "latest_attempt_reason": "CODEX_SCHEMA_INVALID",
        "latest_valid_at": assessment.available_at.isoformat(),
        "rejected_attempt_count_24h": 1,
        "execution_count_24h": 1,
        "final_success_count_24h": 0,
        "first_attempt_success_count_24h": 0,
        "rejection_reasons": ["AI 输出格式不符合契约"],
    }
    assert assessment_detail.status_code == 200
    assert assessment_detail.json()["evidence_count"] == 1
    assert assessment_detail.json()["views"][0]["direction"] == "UNCERTAIN"
    assert assessment_detail.json()["views"][0]["outcome"] is None
    assert assessment_detail.json()["drivers"][0]["evidence"] == [
        {
            "evidence_id": "d" * 64,
            "kind": "INTELLIGENCE_EVENT",
            "title": "重要政策日程发生变化",
            "detail": "该事件用于验证世界认知引用与输入快照来自同一冻结 Packet。",
            "source": "official-calendar",
            "at": (as_of - timedelta(minutes=5)).isoformat(),
        }
    ]
    assert assessment_detail.json()["event_references"][0]["impact_state"] == "ACTIVE"
    assert assessment_detail.json()["input_snapshot"]["analysis_scope"] == (
        packet.analysis_scope
    )
    assert "packet_id" not in assessment_detail.json()["input_snapshot"]
    assert assessment_detail.json()["input_snapshot"]["capacity_summary"] == {
        "missing_fact_count": len(packet.missing_fact_revision_ids),
        "omitted_fact_count": len(packet.omitted_fact_revision_ids),
        "omitted_intelligence_event_count": len(packet.omitted_intelligence_event_refs),
    }
    assert "omitted_intelligence_event_refs" not in assessment_detail.json()[
        "input_snapshot"
    ]
    assert bad_assessment_detail.status_code == 200
    assert bad_assessment_detail.json()["mechanism"] == bad_assessment.market_mechanism
    assert capital_rows.status_code == 200
    assert capital_rows.json() == {"actions": [], "next_cursor": None}
    assert events.status_code == 200
    assert any(
        item["title"] == "Assessment 库中的一手事件"
        for item in events.json()["events"]
    )

    async def read_assessment_pages():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            first = await client.get("/api/assessment/records?limit=1")
            second = await client.get(
                "/api/assessment/records",
                params={"limit": 1, "cursor": first.json()["next_cursor"]},
            )
            return first, second

    first_assessment_page, second_assessment_page = asyncio.run(
        read_assessment_pages()
    )
    assert first_assessment_page.json()["assessments"][0]["assessment_id"] == (
        bad_assessment.assessment_id
    )
    assert second_assessment_page.json()["assessments"][0]["assessment_id"] == (
        assessment.assessment_id
    )


def test_reader_and_serializer_render_a_real_persisted_cycle(app_config, replay_input) -> None:
    engine, result = _seed_cycle(app_config, replay_input)
    reader = DashboardReader(engine, app_config)

    rows = reader.list_cycles(cursor=None, limit=10)
    assert len(rows) == 1
    row_dto = ser.cycle_row(rows[0])
    assert row_dto["cycle_id"] == result.cycle_id
    assert row_dto["symbol"]  # market_snapshots join filled the symbol
    assert row_dto["summary"]  # 一句人话摘要非空
    assert row_dto["category"] in {"exec", "pending", "rejected", "no-trade", "no-action"}


def test_cycle_api_composite_cursor_has_no_gap_during_concurrent_insert(
    app_config,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'cursor-cycles.db'}"
    engine = create_engine(database_url)
    create_schema(engine)
    at = datetime(2026, 8, 22, 3, tzinfo=UTC)

    def insert_cycle(cycle_id: str) -> None:
        with engine.begin() as connection:
            connection.execute(
                insert(analysis_cycles).values(
                    cycle_id=cycle_id,
                    as_of=at,
                    pipeline_version=app_config.pipeline.version,
                    outcome="NO_ACTION",
                    reason_code="NO_ACTION",
                    created_at=at,
                )
            )
            connection.execute(
                insert(market_snapshots).values(
                    cycle_id=cycle_id,
                    symbol="BTCUSDT",
                    as_of=at,
                    content_hash=cycle_id.removeprefix("cycle-").ljust(64, "0")[:64],
                    payload={},
                )
            )

    for suffix in ("a", "b", "c"):
        insert_cycle(f"cycle-{suffix}")
    application = create_app(app_config, database_url)

    async def read_pages():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            first = await client.get("/api/cycles?limit=2")
            assert first.status_code == 200, first.text
            assert "next_cursor" in first.json(), first.text
            insert_cycle("cycle-d")
            cursor = first.json()["next_cursor"]
            second = await client.get("/api/cycles", params={"limit": 2, "cursor": cursor})
            fresh = await client.get("/api/cycles?limit=2")
            invalid = await client.get("/api/cycles?cursor=not-a-valid-cursor")
            return first, second, fresh, invalid

    first, second, fresh, invalid = asyncio.run(read_pages())
    assert [item["cycle_id"] for item in first.json()["cycles"]] == [
        "cycle-c",
        "cycle-b",
    ]
    assert [item["cycle_id"] for item in second.json()["cycles"]] == ["cycle-a"]
    assert second.json()["next_cursor"] is None
    assert [item["cycle_id"] for item in fresh.json()["cycles"]] == [
        "cycle-d",
        "cycle-c",
    ]
    assert invalid.status_code == 400


def test_event_api_composite_cursor_merges_databases_without_gap(
    app_config,
    tmp_path,
) -> None:
    primary_url = f"sqlite+pysqlite:///{tmp_path / 'cursor-events-primary.db'}"
    archive_url = f"sqlite+pysqlite:///{tmp_path / 'cursor-events-archive.db'}"
    primary = create_engine(primary_url)
    archive = create_engine(archive_url)
    create_schema(primary)
    create_schema(archive)
    at = datetime(2026, 8, 22, 3, tzinfo=UTC)

    def put(engine, suffix: str) -> None:
        SqlEventStore(engine, pipeline_id=app_config.pipeline.version).put(
            IntelligenceEvent(
                evidence_id=f"event-{suffix}",
                event_time=at,
                observed_at=at,
                source="official-test",
                title=f"事件 {suffix}",
                body="用于验证跨库稳定游标。",
                symbols=("BTCUSDT",),
                relevance=Decimal("0.8"),
                impact=Decimal("0.7"),
                source_reliability=Decimal("0.9"),
                novelty=Decimal("0.8"),
            )
        )

    put(primary, "a")
    put(archive, "b")
    put(primary, "c")
    application = create_app(
        app_config,
        primary_url,
        assessment_database_url=archive_url,
        assessment_config=app_config,
    )

    async def read_pages():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            first = await client.get("/api/events?limit=2")
            put(archive, "d")
            second = await client.get(
                "/api/events",
                params={"limit": 2, "cursor": first.json()["next_cursor"]},
            )
            return first, second

    first, second = asyncio.run(read_pages())
    assert [item["event_id"] for item in first.json()["events"]] == [
        "NEWS:event-c",
        "NEWS:event-b",
    ]
    assert [item["event_id"] for item in second.json()["events"]] == [
        "NEWS:event-a"
    ]
    assert second.json()["next_cursor"] is None


def test_dashboard_accounts_use_the_assessment_archive_identity(app_config, tmp_path) -> None:
    primary_url = f"sqlite+pysqlite:///{tmp_path / 'accounts-primary.db'}"
    assessment_url = f"sqlite+pysqlite:///{tmp_path / 'accounts-assessment.db'}"
    create_schema(create_engine(primary_url))
    create_schema(create_engine(assessment_url))

    def config_with_account(account_id: str):
        return app_config.model_copy(
            update={
                "codex_accounts": CodexAccountRegistry(
                    version=f"{account_id}-registry-v1",
                    accounts=(
                        CodexAccount(
                            account_id=account_id,
                            codex_home=Path(f"/tmp/{account_id}"),
                            enabled=True,
                        ),
                    ),
                )
            }
        )

    application = create_app(
        config_with_account(".codex-primary"),
        primary_url,
        assessment_database_url=assessment_url,
        assessment_config=config_with_account(".codex-assessment"),
    )

    async def read_accounts():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            return await client.get("/api/accounts")

    response = asyncio.run(read_accounts())

    assert response.status_code == 200
    assert [item["account_id"] for item in response.json()["accounts"]] == [
        ".codex-assessment"
    ]


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
    events = [ser.world_event(event) for event in reader.list_events(cursor=None, limit=20)]
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
        for item in DashboardReader(engine, app_config).list_events(cursor=None, limit=20)
        if item.kind == "AGENT_WAKEUP"
    )

    assert event.source == "主 Agent"
    assert event.title == "BTCUSDT · 请求原因：Dashboard 立即复核"
    assert event.impact is None
    assert event.priority == 100


def test_wrong_store_pipeline_is_quarantined_from_dashboard_without_deletion(
    app_config,
    replay_input,
) -> None:
    engine, _ = _seed_cycle(app_config, replay_input)
    now = replay_input.market.as_of
    wrong_pipeline = "context-assessment-wrong-store-v1"
    SqlTriggerRepository(engine, app_config.trigger).record_trigger(
        build_trigger_event(
            trigger_type=AnalysisTriggerType.CANONICAL_FACT_REVISED,
            symbol="BTCUSDT",
            pipeline_id=wrong_pipeline,
            occurred_at=now,
            observed_at=now,
            priority=100,
            dedup_key="wrong-store-trigger",
        )
    )
    require_fact_store_role(engine, FactStoreRole.CAPITAL, claim_if_missing=True)
    quarantines = SqlFactCohortQuarantineStore(engine)
    store_id, observed_role = quarantines.current_identity()
    quarantines.record(
        build_fact_cohort_quarantine(
            store_id=store_id,
            observed_role=observed_role,
            expected_role=FactStoreRole.CONTEXT,
            manifest_id="release-context-wrong-store-v1",
            pipeline_id=wrong_pipeline,
            analysis_behavior_hash=None,
            quarantined_at=now,
            evidence_ref="review-wrong-store-test",
        )
    )

    events = DashboardReader(engine, app_config).list_events(cursor=None, limit=20)

    assert all(event.kind != "CANONICAL_FACT_REVISED" for event in events)
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(analysis_trigger_events)
        ) == 1
