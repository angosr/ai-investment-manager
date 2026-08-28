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
from sqlalchemy import create_engine, insert

from investment_manager.entrypoints.dashboard import serializers as ser
from investment_manager.entrypoints.dashboard.app import create_app
from investment_manager.entrypoints.dashboard.read_models import AssessmentRecord, DashboardReader
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.executor import (
    AssessmentExecution,
    AssessmentExecutionStatus,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextCausalNode,
    ContextEventImpactState,
    ContextEventReference,
    ContextMechanism,
    ContextMechanismRelationship,
    ContextTransmissionStage,
    ContextVerificationPredicate,
    ContextVerificationTest,
)
from investment_manager.forecast.policy import CodexAccount, CodexAccountRegistry
from investment_manager.forecast.tables import codex_runs, context_assessments
from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.repository import SqlEventStore
from investment_manager.scheduling.models import AnalysisTriggerType, build_trigger_event
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.schema import create_schema
from investment_manager.state.decision.packet import (
    DecisionPacket,
    MandateExposure,
    PacketAssetState,
    PacketDelta,
    PacketIntelligenceEvent,
    PacketPortfolioState,
    RequiredView,
)
from investment_manager.state.tables import decision_packets


def _empty_engine(database_url: str | None = None):
    engine = create_engine(database_url or "sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine


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
        mandate_exposures=(MandateExposure(economic_exposure="CRYPTO_NETWORK", asset="BTC"),),
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
        data_quality_codes=(),
        coverage_gap_codes=(),
        missing_fact_revision_ids=(),
        omitted_fact_revision_ids=(),
    )


def _dashboard_world_model(
    packet: DecisionPacket,
    *,
    assessment_id: str,
    analysis_behavior_hash: str,
    synthesis: str,
    event_reference: ContextEventReference | None = None,
) -> ContextAssessment:
    evidence_id = "d" * 64
    return ContextAssessment(
        assessment_id=assessment_id,
        analysis_scope=packet.analysis_scope,
        mandate_version=packet.mandate_version,
        as_of=packet.as_of,
        available_at=packet.as_of + timedelta(seconds=10),
        analysis_behavior_hash=analysis_behavior_hash,
        decision_packet_hash=packet.content_hash,
        trigger_ids=packet.trigger_ids,
        synthesis=synthesis,
        synthesis_horizon_hours=72,
        mechanisms=(
            ContextMechanism(
                mechanism_id=f"{assessment_id}-mechanism",
                relationship=ContextMechanismRelationship.SUPPORTS,
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
                transmission_stage=ContextTransmissionStage.PROPAGATING,
                verification_tests=(
                    ContextVerificationTest(
                        feature_selector="asset_state:BTC.return_fraction",
                        evaluation_window_minutes=60,
                        supports_predicate=ContextVerificationPredicate(
                            operator="GT", value=Decimal("0")
                        ),
                        contradicts_predicate=ContextVerificationPredicate(
                            operator="LT", value=Decimal("0")
                        ),
                    ),
                ),
                invalidation_conditions=("官方取消日程且风险资产未发生同步响应",),
                next_review_at=packet.as_of + timedelta(hours=6),
            ),
        ),
        event_references=((event_reference,) if event_reference is not None else ()),
    )


def test_dashboard_revalidates_shell_and_caches_hashed_assets(
    app_config,
    tmp_path: Path,
) -> None:
    web_dist = tmp_path / "web-dist"
    assets = web_dist / "assets"
    assets.mkdir(parents=True)
    (web_dist / "index.html").write_text(
        '<script src="/assets/index-contenthash.js"></script>',
        encoding="utf-8",
    )
    (assets / "index-contenthash.js").write_text("export {};", encoding="utf-8")
    application = create_app(
        app_config,
        f"sqlite+pysqlite:///{tmp_path / 'dashboard.sqlite'}",
        web_dist=web_dist,
    )

    async def request_static_files() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            return await client.get("/"), await client.get("/assets/index-contenthash.js")

    shell, asset = asyncio.run(request_static_files())

    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_world_model_assessment_dto_has_one_traceable_contract() -> None:
    as_of = datetime(2026, 8, 22, 18, tzinfo=UTC)
    packet = _dashboard_assessment_packet(as_of=as_of, analysis_scope="primary-portfolio")
    evidence_id = "d" * 64
    assessment = _dashboard_world_model(
        packet,
        assessment_id="world-model-dashboard",
        analysis_behavior_hash="a" * 64,
        synthesis="政策预期正在改变风险偏好，正式结果是主要反转风险。",
    )

    dto = ser.assessment_detail(AssessmentRecord(assessment=assessment, packet=packet))

    assert dto["schema_version"] == "world-model-assessment-v3"
    assert dto["synthesis"] == assessment.synthesis
    assert dto["mechanisms"][0]["causal_chain"][0]["evidence"][0]["evidence_id"] == evidence_id
    assert dto["cited_evidence"] == dto["mechanisms"][0]["causal_chain"][0]["evidence"]


def test_dashboard_reads_capital_and_assessment_history_from_one_fact_store(
    app_config,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'unified.db'}"
    engine = _empty_engine(database_url)
    as_of = datetime.now(UTC) - timedelta(minutes=1)
    packet = _dashboard_assessment_packet(
        as_of=as_of,
        analysis_scope="primary-portfolio",
    )
    assessment = _dashboard_world_model(
        packet,
        assessment_id="context-assessment-dashboard",
        analysis_behavior_hash=configured_assess_behavior_hash(app_config),
        synthesis="政策日程仍可能经风险偏好改变未来风险溢价。",
        event_reference=ContextEventReference(
            evidence_id="d" * 64,
            source="official-calendar",
            title="重要政策日程发生变化",
            event_time=as_of - timedelta(minutes=5),
            impact_state=ContextEventImpactState.ACTIVE,
            rationale="正式日程尚未结束，未来政策预期仍可能变化。",
        ),
    )
    bad_packet = _dashboard_assessment_packet(
        as_of=as_of + timedelta(seconds=1),
        analysis_scope="primary-portfolio",
    )
    bad_assessment = _dashboard_world_model(
        bad_packet,
        assessment_id="context-assessment-dashboard-invalid-language",
        analysis_behavior_hash="c" * 64,
        synthesis="Accepted evidence suggests rising ETH while 叙事存在残渣。",
    )
    with engine.begin() as connection:
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
                    "payload": assessment.model_dump(mode="json"),
                },
                {
                    "assessment_id": bad_assessment.assessment_id,
                    "packet_id": bad_packet.packet_id,
                    "analysis_scope": bad_assessment.analysis_scope,
                    "available_at": bad_assessment.available_at,
                    "analysis_behavior_hash": bad_assessment.analysis_behavior_hash,
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
                observed_at=bad_assessment.available_at - timedelta(seconds=1),
                payload={
                    "analysis_behavior_hash": configured_assess_behavior_hash(app_config),
                    "completed_at": bad_assessment.available_at.isoformat(),
                },
            )
        )
    SqlContextAssessmentStore(engine).record_execution(
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
        engine,
        pipeline_id=app_config.pipeline.version,
    ).put(
        IntelligenceEvent(
            evidence_id="dashboard-assessment-news",
            event_time=assessment.as_of,
            observed_at=assessment.available_at,
            source="official-calendar",
            title="统一事实库中的一手事件",
            body="用于确认观测台读取唯一权威事实链。",
            symbols=("BTCUSDT",),
            relevance=Decimal("0.8"),
            impact=Decimal("0.7"),
            source_reliability=Decimal("0.9"),
            novelty=Decimal("0.8"),
        )
    )

    application = create_app(app_config, database_url)

    async def read_endpoints():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            return await asyncio.gather(
                client.get("/api/assessment/records"),
                client.get(f"/api/assessment/records/{assessment.assessment_id}"),
                client.get(f"/api/assessment/records/{bad_assessment.assessment_id}"),
                client.get("/api/capital/activity"),
                client.get("/api/capital"),
                client.get("/api/evaluation/forecast"),
                client.get("/api/events"),
            )

    (
        assessment_rows,
        assessment_detail,
        bad_assessment_detail,
        capital_rows,
        capital_overview,
        forecast_evidence,
        events,
    ) = asyncio.run(read_endpoints())

    assert assessment_rows.status_code == 200
    assert [item["assessment_id"] for item in assessment_rows.json()["assessments"][:2]] == [
        bad_assessment.assessment_id,
        assessment.assessment_id,
    ]
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
    assert assessment_detail.json()["mechanisms"][0]["causal_chain"][0]["evidence"] == [
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
    assert assessment_detail.json()["input_snapshot"]["analysis_scope"] == (packet.analysis_scope)
    assert "packet_id" not in assessment_detail.json()["input_snapshot"]
    assert "capacity_summary" not in assessment_detail.json()["input_snapshot"]
    assert "omitted_intelligence_event_refs" not in assessment_detail.json()["input_snapshot"]
    assert bad_assessment_detail.status_code == 200
    assert capital_overview.status_code == 200
    assert "forecast_evidence" not in capital_overview.json()
    assert forecast_evidence.status_code == 200
    assert forecast_evidence.json() == {
        "quant_forecast_evidence": None,
        "quant_context_posterior_evidence": None,
        "quant_context_pair_evidence": None,
        "producer_capital_evidence": None,
        "forecast_stability_evidence": None,
        "product_payoff_evidence": None,
        "capital_choice_evidence": None,
        "trading_cost_evidence": {
            "evaluation_version": "trading-cost-evidence-v1",
            "fill_count": 0,
            "round_trip_count": 0,
            "open_lot_count": 0,
            "gross_turnover": "0",
            "realized_gross_pnl": "0",
            "closed_fee_cost": "0",
            "open_fee_cost": "0",
            "realized_net_pnl": "0",
            "positive_gross_pnl": "0",
            "cost_reversal_round_trip_count": 0,
            "accounting_reconciled": None,
            "closed_fee_to_realized_gross_pnl": None,
            "closed_fee_to_positive_gross_pnl": None,
            "minimum_holding_seconds": None,
            "median_holding_seconds": None,
            "maximum_holding_seconds": None,
        },
    }
    assert bad_assessment_detail.json()["synthesis"] == bad_assessment.synthesis
    assert capital_rows.status_code == 200
    assert capital_rows.json() == {"actions": [], "next_cursor": None}
    assert events.status_code == 200
    assert any(item["title"] == "统一事实库中的一手事件" for item in events.json()["events"])

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

    first_assessment_page, second_assessment_page = asyncio.run(read_assessment_pages())
    assert first_assessment_page.json()["assessments"][0]["assessment_id"] == (
        bad_assessment.assessment_id
    )
    assert second_assessment_page.json()["assessments"][0]["assessment_id"] == (
        assessment.assessment_id
    )


def test_event_api_cursor_pages_one_fact_store_without_gap(
    app_config,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'cursor-events.db'}"
    engine = create_engine(database_url)
    create_schema(engine)
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

    put(engine, "a")
    put(engine, "b")
    put(engine, "c")
    application = create_app(app_config, database_url)

    async def read_pages():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            first = await client.get("/api/events?limit=2")
            put(engine, "d")
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
    assert [item["event_id"] for item in second.json()["events"]] == ["NEWS:event-a"]
    assert second.json()["next_cursor"] is None


def test_event_api_projects_latest_normalized_revision_once(
    app_config,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'canonical-events.db'}"
    engine = create_engine(database_url)
    create_schema(engine)
    at = datetime(2026, 8, 22, 3, tzinfo=UTC)
    first = IntelligenceEvent(
        evidence_id="event-normalizer-v1",
        normalizer_version="normalizer-v1",
        acquisition_route="official-rss-v1",
        event_time=at,
        observed_at=at,
        source="official:test",
        title="同一上游事件",
        body="第一版规范化正文。",
        url="https://example.gov/releases/one",
        symbols=("BTCUSDT",),
        relevance=Decimal("0.8"),
        impact=Decimal("0.7"),
        source_reliability=Decimal("1"),
        novelty=Decimal("0.8"),
    )
    latest = first.model_copy(
        update={
            "evidence_id": "event-normalizer-v2",
            "normalizer_version": "normalizer-v2",
            "observed_at": at + timedelta(minutes=1),
            "body": "第二版规范化正文。",
        }
    )
    store = SqlEventStore(engine, pipeline_id=app_config.pipeline.version)
    assert store.put(first)
    assert store.put(latest)
    application = create_app(app_config, database_url)

    async def read_events():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            return await client.get("/api/events")

    response = asyncio.run(read_events())

    assert response.status_code == 200
    assert [item["event_id"] for item in response.json()["events"]] == [
        "NEWS:event-normalizer-v2"
    ]


def test_dashboard_accounts_use_the_runtime_release_identity(app_config, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'accounts.db'}"
    create_schema(create_engine(database_url))

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

    application = create_app(config_with_account(".codex-runtime"), database_url)

    async def read_accounts():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://dashboard.test",
        ) as client:
            return await client.get("/api/accounts")

    response = asyncio.run(read_accounts())

    assert response.status_code == 200
    assert [item["account_id"] for item in response.json()["accounts"]] == [".codex-runtime"]
    usage = response.json()["token_usage"]
    assert usage["window_days"] == 7
    assert usage["total_tokens"] == 0
    assert len(usage["daily"]) == 7
    assert [item["account_id"] for item in usage["accounts"]] == [".codex-runtime"]


def test_dashboard_token_usage_aggregates_each_account_and_total(app_config) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    account_ids = tuple(account.account_id for account in app_config.codex_accounts.accounts)

    rows = (
        ("today", account_ids[0], now, 1_000_000),
        ("yesterday", account_ids[0], now - timedelta(days=1), 250_000),
        ("window-start", account_ids[1], now - timedelta(days=6), 500_000),
        (
            "before-window",
            account_ids[1],
            now.replace(hour=0) - timedelta(days=6, seconds=1),
            9_000_000,
        ),
        ("future", account_ids[0], now + timedelta(seconds=1), 9_000_000),
        ("retired", ".codex-retired", now, 9_000_000),
    )
    with engine.begin() as connection:
        for run_id, account_id, observed_at, tokens in rows:
            connection.execute(
                insert(codex_runs).values(
                    run_id=f"run-token-usage-{run_id}",
                    cycle_id=f"cycle-token-usage-{run_id}",
                    account_id=account_id,
                    attempt=1,
                    status="SUCCEEDED",
                    error_class=None,
                    observed_at=observed_at,
                    payload={"usage": {"total_tokens": tokens}},
                )
            )

    usage = DashboardReader(engine, app_config).ai_token_usage(now=now)
    payload = ser.token_usage(usage)

    assert payload["start_date"] == "2026-08-20"
    assert payload["end_date"] == "2026-08-26"
    assert payload["total_tokens"] == 1_750_000
    assert [item["total_tokens"] for item in payload["daily"]] == [
        500_000,
        0,
        0,
        0,
        0,
        250_000,
        1_000_000,
    ]
    by_account = {item["account_id"]: item for item in payload["accounts"]}
    assert by_account[account_ids[0]]["total_tokens"] == 1_250_000
    assert by_account[account_ids[1]]["total_tokens"] == 500_000
    assert by_account[account_ids[2]]["total_tokens"] == 0


def test_account_status_runs_against_real_engine(app_config) -> None:
    engine = _empty_engine()
    reader = DashboardReader(engine, app_config)
    now = datetime.now(UTC) + timedelta(hours=1)

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
                    "observed_at": completed_at - timedelta(seconds=30),
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
                    "observed_at": completed_at - timedelta(minutes=30),
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
                payload={},
            )
        )

    status = DashboardReader(engine, config).analysis_runtime_status(now=now)

    assert status.recent_attempts == 1
    assert status.recent_successes == 1
    assert status.overdue_forecast_count == 0
    assert {scope.latest_success_at for scope in status.scopes} == {completed_at}


def test_dashboard_call_activity_reads_actual_codex_attempts(app_config, replay_input) -> None:
    engine = _empty_engine()
    now = replay_input.market.as_of
    with engine.begin() as connection:
        for index, observed_at in enumerate(
            (now - timedelta(minutes=59), now, now + timedelta(seconds=1)),
            start=1,
        ):
            connection.execute(
                insert(codex_runs).values(
                    run_id=f"run-activity-{index}",
                    cycle_id=f"cycle-activity-{index}",
                    account_id=".codex-test",
                    attempt=1,
                    status="SUCCEEDED",
                    error_class=None,
                    observed_at=observed_at,
                    payload={"observed_at": observed_at.isoformat()},
                )
            )

    assert DashboardReader(engine, app_config).ai_calls_last_hour(now=now) == 2


def test_news_fed_into_current_ai_packet_links_back_to_it(app_config, replay_input) -> None:
    engine = _empty_engine()
    now = replay_input.market.as_of
    packet = _dashboard_assessment_packet(
        as_of=now,
        analysis_scope=app_config.assessment.mandate.analysis_scope,
    )
    evidence_id = packet.intelligence_events[0].evidence_id
    with engine.begin() as connection:
        connection.execute(
            insert(decision_packets).values(
                packet_id=packet.packet_id,
                analysis_scope=packet.analysis_scope,
                as_of=packet.as_of,
                policy_version=packet.policy_version,
                content_hash=packet.content_hash,
                payload=packet.model_dump(mode="json"),
            )
        )

    # 补一条与 Packet 中证据同 id 的采集事件，模拟它确实进入了冻结 AI 输入。
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
    assert fed["fed_cycle_id"] == packet.packet_id
    assert fed["fed_cycle_at"] is not None


def test_agent_wakeup_is_attributed_to_main_agent(app_config, replay_input) -> None:
    engine = _empty_engine()
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
    assert event.attention_priority is None
    assert event.priority == 100
