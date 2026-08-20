"""观测台投影层单元测试：措辞、摘要拼装、权益累加、健康合成与资源采样。

只测「事实 → DTO」的纯逻辑；DB 取数是既有已测 Repository 的薄封装，不在此重复覆盖。
用轻量替身（SimpleNamespace）隔离领域构造细节，聚焦本层行为。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from investment_manager.cli import _default_web_dist
from investment_manager.dashboard import formatting as fmt
from investment_manager.dashboard import serializers as ser
from investment_manager.dashboard.health import assemble_health
from investment_manager.dashboard.read_models import (
    AccountStatus,
    AnalysisRuntimeStatus,
    AnalysisScopeRuntimeStatus,
    EquityWindow,
    WorldEvent,
)
from investment_manager.dashboard.resources import sample_host_resources
from investment_manager.execution.models import Side


def _intent(side: Side) -> SimpleNamespace:
    return SimpleNamespace(
        side=side,
        entry=SimpleNamespace(price=Decimal("63140")),
        stop_price=Decimal("61980"),
    )


def _analysis_status(now: datetime, **updates) -> AnalysisRuntimeStatus:
    latest = updates.pop("latest_success_at", now)
    values = {
        "recent_attempts": 1,
        "recent_successes": 1,
        "pending_outbox_count": 0,
        "oldest_pending_outbox_at": None,
        "release_aligned": True,
        "overdue_forecast_count": 0,
        "oldest_overdue_analysis_at": None,
        "scopes": (
            AnalysisScopeRuntimeStatus(
                symbol="BTCUSDT",
                latest_success_at=latest,
                heartbeat_seconds=900,
            ),
        ),
    }
    values.update(updates)
    return AnalysisRuntimeStatus(**values)


def test_health_uses_authoritative_per_scope_heartbeat() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    healthy_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now,
            scopes=(
                AnalysisScopeRuntimeStatus(
                    symbol="BTCUSDT",
                    latest_success_at=now - timedelta(minutes=40),
                    heartbeat_seconds=3600,
                ),
            ),
        ),
    )
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now,
            latest_success_at=now - timedelta(minutes=40),
            scopes=(
                AnalysisScopeRuntimeStatus(
                    symbol="BTCUSDT",
                    latest_success_at=now - timedelta(minutes=40),
                    heartbeat_seconds=3600,
                ),
                AnalysisScopeRuntimeStatus(
                    symbol="ETHUSDT",
                    latest_success_at=None,
                    heartbeat_seconds=3600,
                ),
            ),
        ),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    healthy = assemble_health(healthy_reader, config, now=now)
    result = assemble_health(reader, config, now=now)
    healthy_check = next(
        item for item in healthy["checks"] if item["key"] == "ai_analysis"
    )
    check = next(item for item in result["checks"] if item["key"] == "ai_analysis")

    assert healthy_check["state"] == "ok"
    assert "2400/3900 秒" in healthy_check["detail"]
    assert check["state"] == "unknown"
    assert "ETHUSDT 等待当前版本首次分析" in check["detail"]


def _health_policy_extras() -> dict:
    return {
        "trigger": SimpleNamespace(
            heartbeat_minutes=15,
            outbox_fallback_poll_seconds=1,
        ),
        "shadow": SimpleNamespace(analysis_deadline_seconds=300),
    }


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
        outcomes=outcomes,
        metrics=report,
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


@pytest.mark.parametrize(
    ("enabled", "healthy", "leased", "expected"),
    [
        (False, None, False, "DISABLED"),
        (True, None, False, "ENABLED"),
        (True, True, False, "HEALTHY"),
        (True, False, False, "COOLDOWN"),
        (True, None, True, "LEASED"),
    ],
)
def test_account_state_keeps_enabled_distinct_from_unprobed_health(
    enabled: bool,
    healthy: bool | None,
    leased: bool,
    expected: str,
):
    status = AccountStatus(
        account_id=".codex-test",
        enabled=enabled,
        headroom_percent=None,
        healthy=healthy,
        observed_at=None,
        leased=leased,
        recent_failures=0,
    )

    assert ser.account_status(status)["state"] == expected


def test_health_is_unknown_without_data_and_bad_on_mismatch():
    now = datetime.now(UTC)
    empty_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: None,
        latest_market_observed_at=lambda: None,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now, latest_success_at=None, release_aligned=None
        ),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=30,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )
    result = assemble_health(empty_reader, config, now=now)
    assert result["overall"] == "unknown"

    mismatch = SimpleNamespace(status="MISMATCH", freeze_new_risk=True, as_of=now)
    bad_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: mismatch,
        latest_market_observed_at=lambda: None,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    bad = assemble_health(bad_reader, config, now=now)
    assert bad["overall"] == "bad"
    reconciliation = next(c for c in bad["checks"] if c["key"] == "reconciliation")
    assert reconciliation["state"] == "bad"


def test_health_ages_persisted_freshness_and_uses_real_kill_switch():
    observed_at = datetime(2026, 8, 18, 12, tzinfo=UTC)
    now = observed_at.replace(minute=2)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: observed_at,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=True,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(reader, config, now=now)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["data_freshness"]["state"] == "bad"
    assert checks["kill_switch"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_reads_persisted_portfolio_kill_switch() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: True,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(reader, config, now=now)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["kill_switch"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_surfaces_control_plane_backlog_and_release_drift() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now,
            pending_outbox_count=3,
            oldest_pending_outbox_at=now - timedelta(seconds=10),
            release_aligned=False,
            overdue_forecast_count=2,
            oldest_overdue_analysis_at=now - timedelta(hours=5),
        ),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(reader, config, now=now)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["trigger_delivery"]["state"] == "bad"
    assert checks["forecast_settlement"]["state"] == "bad"
    assert checks["release_alignment"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_reads_temporal_coordinator_pending_state() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        coordinator_statuses=(
            {"symbol": "BTCUSDT", "pending_count": 0, "active_batch_id": None},
            {"symbol": "ETHUSDT", "pending_count": 4, "active_batch_id": None},
        ),
    )

    check = next(item for item in result["checks"] if item["key"] == "trigger_coordinator")
    assert check["state"] == "warn"
    assert "状态不明 4 条" in check["detail"]
    assert result["overall"] == "warn"


def test_health_treats_policy_deferred_triggers_as_healthy() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        coordinator_statuses=(
            {
                "symbol": "BTCUSDT",
                "pending_count": 2,
                "active_batch_id": None,
                "next_reconsider_at": (now + timedelta(minutes=5)).isoformat(),
            },
            {
                "symbol": "ETHUSDT",
                "pending_count": 3,
                "active_batch_id": "batch-active",
                "next_reconsider_at": None,
            },
        ),
    )

    check = next(item for item in result["checks"] if item["key"] == "trigger_coordinator")
    assert check == {
        "key": "trigger_coordinator",
        "name": "触发协调器",
        "state": "ok",
        "detail": "正常等待 5 条 · 执行中 1 批",
    }
    assert result["overall"] == "ok"


@pytest.mark.parametrize(
    ("percent", "expected"),
    ((89.9, "ok"), (90.0, "warn"), (95.0, "bad")),
)
def test_health_surfaces_host_disk_pressure(percent, expected) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        host_resources={"disk": {"percent": percent}},
    )

    disk = next(item for item in result["checks"] if item["key"] == "host_disk")
    assert disk["state"] == expected
    assert result["overall"] == expected


def test_sample_host_resources_shape():
    sample = sample_host_resources()
    assert 0.0 <= sample["cpu_percent"] <= 100.0
    assert set(sample["memory"]) == {"used_bytes", "total_bytes", "percent"}
    assert set(sample["disk"]) == {"used_bytes", "total_bytes", "percent"}
    assert set(sample["load_average"]) == {"1m", "5m", "15m"}


def test_default_web_dist_does_not_depend_on_process_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    repository = tmp_path / "repository"
    dist = repository / "web" / "dist"
    dist.mkdir(parents=True)
    runtime_directory = tmp_path / "systemd-runtime"
    runtime_directory.mkdir()
    monkeypatch.chdir(runtime_directory)
    monkeypatch.setattr(
        "investment_manager.cli.__file__",
        str(repository / "src" / "investment_manager" / "cli.py"),
    )

    assert _default_web_dist() == dist.resolve()
