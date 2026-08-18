"""集成测试：跑真实回放周期落库，再经观测台读取层+投影层还原为 DTO。

用真实持久化的 payload（而非替身）验证 read_models 与 serializers，覆盖字段名一致性、
周期轨推断与信息快照投影。SQLite 内存库，无外部依赖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from quant_core.cycle import AnalysisCycle
from quant_core.dashboard import serializers as ser
from quant_core.dashboard.read_models import DashboardReader
from quant_core.domain import IntelligenceEvent
from quant_core.execution import MockExchange
from quant_core.persistence import (
    SqlEventStore,
    SqlFactLedger,
    SqlRiskBudgetStore,
    create_schema,
)


def _seed_cycle(app_config, replay_input):
    engine = create_engine("sqlite+pysqlite:///:memory:")
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

    # 周期轨：至少到达「面板就绪」；reached 落在合法范围
    assert 1 <= detail["rail"]["reached"] <= 9

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


def test_equity_and_health_run_against_real_engine(app_config, replay_input) -> None:
    engine, _ = _seed_cycle(app_config, replay_input)
    reader = DashboardReader(engine, app_config)
    now = datetime.now(UTC) + timedelta(hours=1)

    equity = ser.equity(reader.equity_window(now=now, hours=48))
    assert equity["trade_count"] >= 0  # 回放未平仓则为 0，结构仍合法

    accounts = [ser.account_status(status) for status in reader.accounts(now=now)]
    assert len(accounts) == 3
    assert all(account["state"] == "DISABLED" for account in accounts)


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
    now = datetime.now(UTC)
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
