"""单一健康状态（异常驱动）：把散落的守卫合成一个结论 + 明细。

正常时前端只显示「运行正常」，异常才点名。缺数据时返回 ``unknown``（失败关闭：不假装
正常，也不误报为红）。数据新鲜度直接读取实时行情投影，账户新鲜度复用同一次对账查询。
"""

from __future__ import annotations

from datetime import datetime

from quant_core.dashboard.read_models import DashboardReader
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
) -> dict:
    report = reader.latest_reconciliation(now=now)  # 一次查询，供三项检查复用
    checks = [
        _reconciliation_check(report, config, now),
        _freeze_check(report, config, now),
        _freshness_check(reader, report, config, now),
        _kill_switch_check(reader, config),
    ]
    if host_resources is not None:
        checks.append(_disk_check(host_resources))
    worst = max(checks, key=lambda check: _SEVERITY[check["state"]])
    overall = worst["state"]
    if overall == "ok":
        headline = "运行正常"
    elif overall == "unknown":
        headline = "等待数据"
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
