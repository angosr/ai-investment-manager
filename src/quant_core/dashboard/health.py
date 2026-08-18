"""单一健康状态（异常驱动）：把散落的守卫合成一个结论 + 明细。

正常时前端只显示「运行正常」，异常才点名。缺数据时返回 ``unknown``（失败关闭：不假装
正常，也不误报为红）。数据/账户新鲜度取最近一个周期的既有指标观测，不另立口径。
"""

from __future__ import annotations

from datetime import datetime

from quant_core.dashboard.read_models import DashboardReader
from quant_core.reconciliation import ReconciliationReport

_SEVERITY = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}


def assemble_health(reader: DashboardReader, config, *, now: datetime) -> dict:
    report = reader.latest_reconciliation(now=now)  # 一次查询，供三项检查复用
    checks = [
        _reconciliation_check(report),
        _freeze_check(report),
        _freshness_check(reader, config),
        _kill_switch_check(report),
    ]
    worst = max(checks, key=lambda check: _SEVERITY[check["state"]])
    overall = worst["state"]
    if overall == "ok":
        headline = "运行正常"
    elif overall == "unknown":
        headline = "等待数据"
    else:
        headline = f"{worst['name']}异常"
    return {"overall": overall, "headline": headline, "checks": checks}


def _reconciliation_check(report: ReconciliationReport | None) -> dict:
    if report is None:
        return _check("reconciliation", "对账", "unknown", "暂无对账报告")
    state = "ok" if report.status == "MATCHED" else "bad"
    return _check("reconciliation", "对账", state, _recon_label(report.status))


def _freeze_check(report: ReconciliationReport | None) -> dict:
    if report is None:
        return _check("risk_budget", "风险预算", "unknown", "暂无对账报告")
    frozen = report.freeze_new_risk
    return _check(
        "risk_budget",
        "风险预算",
        "bad" if frozen else "ok",
        "已冻结新增风险" if frozen else "未冻结",
    )


def _freshness_check(reader: DashboardReader, config) -> dict:
    metrics = reader.latest_cycle_metrics()
    if metrics is None:
        return _check("data_freshness", "数据新鲜度", "unknown", "暂无周期数据")
    market_age = _metric_value(metrics, "market_data_age_seconds")
    if market_age is None:
        return _check("data_freshness", "数据新鲜度", "unknown", "暂无新鲜度指标")
    limit = config.panel.max_market_age_seconds
    stale = float(market_age) > limit
    return _check(
        "data_freshness",
        "数据新鲜度",
        "bad" if stale else "ok",
        f"{market_age} 秒 / 上限 {limit} 秒",
    )


def _kill_switch_check(report: ReconciliationReport | None) -> dict:
    tripped = report is not None and report.freeze_new_risk
    return _check(
        "kill_switch",
        "熔断 Kill Switch",
        "warn" if tripped else "ok",
        "已触发（由风控口径派生）" if tripped else "待命",
    )


def _metric_value(metrics, name: str):
    for observation in metrics:
        if dict(observation.dimensions).get("metric") == name:
            return observation.value
    return None


def _recon_label(status: str) -> str:
    return {"MATCHED": "一致", "MISMATCH": "不一致", "UNKNOWN": "状态未知"}.get(status, status)


def _check(key: str, name: str, state: str, detail: str) -> dict:
    return {"key": key, "name": name, "state": state, "detail": detail}
