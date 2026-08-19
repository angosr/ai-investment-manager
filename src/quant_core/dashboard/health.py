"""单一健康状态（异常驱动）：把散落的守卫合成一个结论 + 明细。

正常时前端只显示「运行正常」，异常才点名。缺数据时返回 ``unknown``（失败关闭：不假装
正常，也不误报为红）。数据新鲜度直接读取实时行情投影，账户新鲜度复用同一次对账查询。
"""

from __future__ import annotations

from datetime import datetime

from quant_core.dashboard.read_models import AnalysisRuntimeStatus, DashboardReader
from quant_core.reconciliation import ReconciliationReport

_SEVERITY = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}
_DISK_WARN_PERCENT = 90
_DISK_BAD_PERCENT = 95


def assemble_health(
    reader: DashboardReader,
    config,
    *,
    now: datetime,
    host_resources: dict | None = None,
    coordinator_statuses: tuple[dict, ...] | None = None,
) -> dict:
    report = reader.latest_reconciliation(now=now)  # 一次查询，供三项检查复用
    analysis = reader.analysis_runtime_status(now=now)
    checks = [
        _reconciliation_check(report, config, now),
        _freeze_check(report, config, now),
        _freshness_check(reader, report, config, now),
        _kill_switch_check(reader, config),
        _analysis_check(analysis, config, now),
        _forecast_settlement_check(analysis, now),
        _trigger_delivery_check(analysis, config, now),
        _release_alignment_check(analysis),
        _call_budget_check(analysis, config),
    ]
    if coordinator_statuses is not None:
        checks.append(_trigger_coordinator_check(coordinator_statuses))
    if host_resources is not None:
        checks.append(_disk_check(host_resources))
    worst = max(checks, key=lambda check: _SEVERITY[check["state"]])
    overall = worst["state"]
    if overall == "ok":
        headline = "运行正常"
    elif overall == "unknown":
        headline = "等待数据"
    elif overall == "warn":
        headline = f"{worst['name']}需关注"
    else:
        headline = f"{worst['name']}异常"
    return {"overall": overall, "headline": headline, "checks": checks}


def _reconciliation_check(report: ReconciliationReport | None, config, now: datetime) -> dict:
    if report is None:
        return _check("reconciliation", "对账", "unknown", "暂无对账报告")
    age = _report_age_seconds(report, now)
    if age is None:
        return _check("reconciliation", "对账", "bad", "报告时间晚于当前时间")
    if age > config.reconciliation.maximum_report_age_seconds:
        return _check("reconciliation", "对账", "bad", f"报告已过期（{int(age)} 秒）")
    state = "ok" if report.status == "MATCHED" else "bad"
    return _check("reconciliation", "对账", state, _recon_label(report.status))


def _freeze_check(report: ReconciliationReport | None, config, now: datetime) -> dict:
    if report is None:
        return _check("risk_budget", "风险预算", "unknown", "暂无对账报告")
    age = _report_age_seconds(report, now)
    if age is None or age > config.reconciliation.maximum_report_age_seconds:
        return _check("risk_budget", "风险预算", "bad", "对账不可用，按冻结处理")
    frozen = report.freeze_new_risk
    return _check(
        "risk_budget",
        "风险预算",
        "bad" if frozen else "ok",
        "已冻结新增风险" if frozen else "未冻结",
    )


def _freshness_check(
    reader: DashboardReader,
    report: ReconciliationReport | None,
    config,
    now: datetime,
) -> dict:
    market_observed_at = reader.latest_market_observed_at()
    if market_observed_at is None:
        return _check("data_freshness", "数据新鲜度", "unknown", "实时行情尚未就绪")
    if report is None:
        return _check("data_freshness", "数据新鲜度", "unknown", "账户对账尚未就绪")
    market_age = (now - market_observed_at).total_seconds()
    account_age = (now - report.as_of).total_seconds()
    if market_age < 0 or account_age < 0:
        return _check("data_freshness", "数据新鲜度", "bad", "观测时间晚于当前时间")
    market_limit = config.risk.maximum_market_age_seconds
    account_limit = config.risk.maximum_account_age_seconds
    stale = market_age > market_limit or account_age > account_limit
    return _check(
        "data_freshness",
        "数据新鲜度",
        "bad" if stale else "ok",
        f"行情 {int(market_age)}/{market_limit} 秒 · 账户 {int(account_age)}/{account_limit} 秒",
    )


def _kill_switch_check(reader: DashboardReader, config) -> dict:
    persisted = reader.portfolio_protection_active()
    tripped = config.risk.kill_switch or persisted is True
    if tripped:
        detail = "已触发"
        state = "bad"
    elif persisted is None:
        detail = "持久保护状态不可用"
        state = "unknown"
    else:
        detail = "待命"
        state = "ok"
    return _check(
        "kill_switch",
        "熔断 Kill Switch",
        state,
        detail,
    )


def _analysis_check(
    status: AnalysisRuntimeStatus,
    config,
    now: datetime,
) -> dict:
    latest = status.latest_success_at
    if latest is None:
        return _check("ai_analysis", "AI 分析", "unknown", "当前版本尚无成功分析")
    age = (now - latest).total_seconds()
    if age < 0:
        return _check("ai_analysis", "AI 分析", "bad", "完成时间晚于当前时间")
    expected_seconds = (
        config.trigger.heartbeat_minutes * 60 + config.shadow.analysis_deadline_seconds
    )
    if age > expected_seconds * 2:
        state = "bad"
    elif age > expected_seconds or (
        status.recent_attempts >= 3
        and status.recent_successes < status.recent_attempts
    ):
        state = "warn"
    else:
        state = "ok"
    return _check(
        "ai_analysis",
        "AI 分析",
        state,
        f"最近成功 {int(age)} 秒 · 近 1h {status.recent_successes}/{status.recent_attempts} 成功",
    )


def _trigger_delivery_check(
    status: AnalysisRuntimeStatus,
    config,
    now: datetime,
) -> dict:
    if status.pending_outbox_count == 0:
        return _check("trigger_delivery", "触发投递", "ok", "无到期待投递")
    oldest = status.oldest_pending_outbox_at
    if oldest is None:
        return _check("trigger_delivery", "触发投递", "unknown", "积压时间不可用")
    age = (now - oldest).total_seconds()
    if age < 0:
        return _check("trigger_delivery", "触发投递", "bad", "投递时间晚于当前时间")
    tolerance = max(5.0, config.trigger.outbox_fallback_poll_seconds * 5)
    state = "bad" if age > tolerance else "warn"
    return _check(
        "trigger_delivery",
        "触发投递",
        state,
        f"{status.pending_outbox_count} 条 · 最久 {int(age)} 秒",
    )


def _forecast_settlement_check(
    status: AnalysisRuntimeStatus,
    now: datetime,
) -> dict:
    count = status.overdue_forecast_count
    if count == 0:
        return _check("forecast_settlement", "预测结算", "ok", "无逾期预测")
    oldest = status.oldest_overdue_analysis_at
    if oldest is None:
        return _check("forecast_settlement", "预测结算", "unknown", "逾期时间不可用")
    age = (now - oldest).total_seconds()
    if age < 0:
        return _check("forecast_settlement", "预测结算", "bad", "分析时间晚于当前时间")
    return _check(
        "forecast_settlement",
        "预测结算",
        "bad",
        f"{count} 个 Proposal 未完整结算 · 最久 {int(age)} 秒",
    )


def _release_alignment_check(status: AnalysisRuntimeStatus) -> dict:
    if status.release_aligned is None:
        return _check("release_alignment", "版本一致性", "unknown", "发布或计划事实缺失")
    return _check(
        "release_alignment",
        "版本一致性",
        "ok" if status.release_aligned else "bad",
        "一致" if status.release_aligned else "运行配置与发布事实不一致",
    )


def _trigger_coordinator_check(statuses: tuple[dict, ...]) -> dict:
    if not statuses or any("error" in item for item in statuses):
        return _check(
            "trigger_coordinator",
            "触发协调器",
            "bad",
            "Temporal 状态不可用",
        )
    pending = sum(int(item.get("pending_count", 0)) for item in statuses)
    active = sum(item.get("active_batch_id") is not None for item in statuses)
    if pending:
        return _check(
            "trigger_coordinator",
            "触发协调器",
            "warn",
            f"等待 {pending} 条 · 执行中 {active} 批",
        )
    return _check(
        "trigger_coordinator",
        "触发协调器",
        "ok",
        f"无积压 · 执行中 {active} 批",
    )


def _call_budget_check(status: AnalysisRuntimeStatus, config) -> dict:
    used = status.calls_last_hour
    cap = config.trigger.maximum_ai_calls_per_hour
    if used > cap:
        state = "bad"
    elif used == cap:
        state = "warn"
    else:
        state = "ok"
    return _check(
        "ai_call_budget",
        "AI 调用预算",
        state,
        f"{used}/{cap}" + ("，等待滚动释放" if used == cap else ""),
    )


def _disk_check(host_resources: dict) -> dict:
    disk = host_resources.get("disk")
    percent = disk.get("percent") if isinstance(disk, dict) else None
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return _check("host_disk", "主机磁盘", "unknown", "磁盘占用不可用")
    if percent >= _DISK_BAD_PERCENT:
        state = "bad"
    elif percent >= _DISK_WARN_PERCENT:
        state = "warn"
    else:
        state = "ok"
    return _check("host_disk", "主机磁盘", state, f"已使用 {percent:.1f}%")


def _report_age_seconds(report: ReconciliationReport, now: datetime) -> float | None:
    age = (now - report.as_of).total_seconds()
    return age if age >= 0 else None


def _recon_label(status: str) -> str:
    return {"MATCHED": "一致", "MISMATCH": "不一致", "UNKNOWN": "状态未知"}.get(status, status)


def _check(key: str, name: str, state: str, detail: str) -> dict:
    return {"key": key, "name": name, "state": state, "detail": detail}
