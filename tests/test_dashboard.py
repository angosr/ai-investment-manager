"""观测台投影层单元测试：措辞、摘要拼装、权益累加、健康合成与资源采样。

只测「事实 → DTO」的纯逻辑；DB 取数是既有已测 Repository 的薄封装，不在此重复覆盖。
用轻量替身（SimpleNamespace）隔离领域构造细节，聚焦本层行为。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from quant_core.dashboard import formatting as fmt
from quant_core.dashboard import serializers as ser
from quant_core.dashboard.health import assemble_health
from quant_core.dashboard.read_models import EquityWindow, WorldEvent
from quant_core.dashboard.resources import sample_host_resources
from quant_core.domain import Side


def _intent(side: Side) -> SimpleNamespace:
    return SimpleNamespace(
        side=side,
        entry=SimpleNamespace(price=Decimal("63140")),
        stop_price=Decimal("61980"),
    )


def test_direction_label_maps_side():
    assert fmt.direction_label(Side.BUY) == "多"
    assert fmt.direction_label(Side.SELL) == "空"
    assert fmt.direction_label(None) is None


def test_reason_plain_falls_back_to_raw_code():
    assert fmt.reason_plain("INSUFFICIENT_NET_EDGE") == "扣掉成本后优势不足"
    assert fmt.reason_plain("SOME_NEW_CODE") == "SOME_NEW_CODE"


def test_thesis_gist_truncates_and_prefixes():
    proposal = SimpleNamespace(thesis="第一行理由\n第二行不该出现")
    gist = fmt.thesis_gist(proposal, limit=10)
    assert gist.startswith("AI：第一行理由")
    assert "第二行" not in gist


def test_compose_summary_per_outcome():
    assert (
        fmt.compose_summary(outcome="EXECUTED", reason_code="", intent=_intent(Side.BUY))
        == "开多仓 @ 63140，止损 61980"
    )
    assert (
        fmt.compose_summary(
            outcome="RISK_REJECTED", reason_code="PORTFOLIO_RISK_BUDGET_EXHAUSTED", intent=None
        )
        == "未开仓 · 风控拒绝：组合风险超限"
    )
    assert (
        fmt.compose_summary(outcome="NO_TRADE", reason_code="INSUFFICIENT_NET_EDGE", intent=None)
        == "未开仓 · 扣掉成本后优势不足"
    )
    assert fmt.compose_summary(outcome="NO_ACTION", reason_code="", intent=None).startswith(
        "未行动"
    )


def test_equity_curve_is_running_sum_of_net_pnl():
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    outcomes = (
        SimpleNamespace(net_pnl=Decimal("22"), closed_at=now, outcome_id="a"),
        SimpleNamespace(net_pnl=Decimal("-14"), closed_at=now, outcome_id="b"),
        SimpleNamespace(net_pnl=Decimal("31"), closed_at=now, outcome_id="c"),
    )
    report = SimpleNamespace(
        window_start=now,
        window_end=now,
        net_pnl=Decimal("39"),
        total_fees=Decimal("0"),
        win_rate=Decimal("0.66"),
        profit_factor=Decimal("2.5"),
        maximum_drawdown=Decimal("14"),
        closed_trade_count=3,
    )
    window = EquityWindow(
        facts=SimpleNamespace(outcomes=outcomes),
        report=report,
        lookback_start=now,
        lookback_end=now,
    )
    result = ser.equity(window)
    assert [point["equity"] for point in result["curve"]] == ["22", "8", "39"]
    assert result["trade_count"] == 3
    assert result["summary"]["net_pnl"] == "39"
    assert result["summary"]["closed_trade_count"] == 3


def test_world_event_serializes_injection_flag():
    event = WorldEvent(
        kind="NEWS",
        at=datetime(2026, 8, 18, 11, 40, tzinfo=UTC),
        source="TrendRadar",
        title="疑似夹带指令的推广内容",
        symbols=("BTCUSDT",),
        impact=0.12,
        injection_suspected=True,
    )
    dto = ser.world_event(event)
    assert dto["injection_suspected"] is True
    assert dto["symbols"] == ["BTCUSDT"]


def test_health_is_unknown_without_data_and_bad_on_mismatch():
    empty_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: None,
        latest_cycle_metrics=lambda: None,
    )
    config = SimpleNamespace(panel=SimpleNamespace(max_market_age_seconds=60))
    result = assemble_health(empty_reader, config, now=datetime.now(UTC))
    assert result["overall"] == "unknown"

    mismatch = SimpleNamespace(status="MISMATCH", freeze_new_risk=True)
    bad_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: mismatch,
        latest_cycle_metrics=lambda: None,
    )
    bad = assemble_health(bad_reader, config, now=datetime.now(UTC))
    assert bad["overall"] == "bad"
    reconciliation = next(c for c in bad["checks"] if c["key"] == "reconciliation")
    assert reconciliation["state"] == "bad"


def test_sample_host_resources_shape():
    sample = sample_host_resources()
    assert 0.0 <= sample["cpu_percent"] <= 100.0
    assert set(sample["memory"]) == {"used_bytes", "total_bytes", "percent"}
    assert set(sample["disk"]) == {"used_bytes", "total_bytes", "percent"}
    assert set(sample["load_average"]) == {"1m", "5m", "15m"}
