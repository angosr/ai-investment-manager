"""单一健康状态（异常驱动）：把散落的守卫合成一个结论 + 明细。

正常时前端只显示「运行正常」，异常才点名。缺数据时返回 ``unknown``（失败关闭：不假装
正常，也不误报为红）。数据新鲜度直接读取实时行情投影，账户新鲜度复用同一次对账查询。
"""

from __future__ import annotations

from datetime import datetime

from investment_manager.entrypoints.dashboard.capital import CapitalOverview
from investment_manager.entrypoints.dashboard.read_models import (
    AnalysisRuntimeStatus,
    DashboardReader,
)
from investment_manager.execution.reconciliation.engine import ReconciliationReport

_SEVERITY = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}
_DISK_WARN_PERCENT = 90
_DISK_BAD_PERCENT = 95


def assemble_health(
    reader: DashboardReader,
    config,
    *,
    now: datetime,
    capital_overview: CapitalOverview | None = None,
    host_resources: dict | None = None,
    coordinator_statuses: tuple[dict, ...] | None = None,
) -> dict:
    analysis = reader.analysis_runtime_status(now=now)
    if getattr(getattr(config, "capital", None), "enabled", False):
        checks = [
            _capital_account_check(capital_overview, config),
            _capital_freshness_check(reader, capital_overview, config, now),
            _capital_decision_check(capital_overview, config, now),
            _capital_execution_check(capital_overview, now),
            _capital_performance_check(capital_overview),
            _forecast_settlement_check(analysis, now),
            _trigger_delivery_check(analysis, config, now),
            _release_alignment_check(analysis),
        ]
    else:
        report = reader.latest_reconciliation(now=now)  # 一次查询，供三项检查复用
        checks = [
            _reconciliation_check(report, config, now),
            _freeze_check(report, config, now),
            _freshness_check(reader, report, config, now),
            _kill_switch_check(reader, config),
            _analysis_check(analysis, config, now),
            _forecast_settlement_check(analysis, now),
            _trigger_delivery_check(analysis, config, now),
            _release_alignment_check(analysis),
        ]
    if coordinator_statuses is not None:
        checks.append(_trigger_coordinator_check(coordinator_statuses, now))
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


def _capital_account_check(
    overview: CapitalOverview | None,
    config,
) -> dict:
    account = overview.account if overview is not None else None
    if account is None:
        return _check("capital_account", "资本账户", "unknown", "等待首次账户快照")
    if not account.reconciled:
        return _check("capital_account", "资本账户", "bad", "账户未对账")
    if account.kill_switch_active or config.capital.risk.kill_switch:
        return _check("capital_account", "资本账户", "bad", "Kill Switch 已触发")
    if account.drawdown_fraction > config.capital.risk.maximum_drawdown_fraction:
        return _check(
            "capital_account",
            "资本账户",
            "bad",
            f"回撤 {account.drawdown_fraction} 超过限制",
        )
    return _check(
        "capital_account",
        "资本账户",
        "ok",
        f"权益 {account.equity} {account.settlement_asset} · 现金 {account.cash_balance}",
    )


def _capital_freshness_check(
    reader: DashboardReader,
    overview: CapitalOverview | None,
    config,
    now: datetime,
) -> dict:
    spot_at = reader.latest_market_observed_at()
    perpetual_at = reader.latest_perpetual_observed_at()
    account = overview.account if overview is not None else None
    if spot_at is None or perpetual_at is None or account is None:
        return _check("capital_freshness", "资本事实新鲜度", "unknown", "事实尚未齐备")
    ages = (
        (now - spot_at).total_seconds(),
        (now - perpetual_at).total_seconds(),
        (now - account.as_of).total_seconds(),
    )
    account_limit = (
        config.trigger.heartbeat_minutes * 60
        + config.shadow.analysis_deadline_seconds
    )
    limits = (
        config.capital.risk.maximum_quote_age_seconds,
        config.capital.risk.maximum_quote_age_seconds,
        account_limit,
    )
    if any(age < 0 for age in ages):
        return _check("capital_freshness", "资本事实新鲜度", "bad", "事实时间晚于当前时间")
    stale = any(age > limit for age, limit in zip(ages, limits, strict=True))
    return _check(
        "capital_freshness",
        "资本事实新鲜度",
        "bad" if stale else "ok",
        (
            f"Spot {int(ages[0])}/{limits[0]} 秒 · "
            f"Perpetual {int(ages[1])}/{limits[1]} 秒 · "
            f"账户 {int(ages[2])}/{limits[2]} 秒"
        ),
    )


def _capital_decision_check(
    overview: CapitalOverview | None,
    config,
    now: datetime,
) -> dict:
    target = overview.target if overview is not None else None
    if target is None:
        return _check("capital_decision", "资本决策", "unknown", "等待首次决策")
    age = (now - target.as_of).total_seconds()
    limit = config.trigger.heartbeat_minutes * 60 + config.shadow.analysis_deadline_seconds
    if age < 0 or age > limit * 2:
        return _check("capital_decision", "资本决策", "bad", f"决策已过期（{int(age)} 秒）")
    reasons = ", ".join(target.reason_codes)
    return _check(
        "capital_decision",
        "资本决策",
        "ok",
        reasons,
    )


def _capital_execution_check(
    overview: CapitalOverview | None,
    now: datetime,
) -> dict:
    groups = overview.active_groups if overview is not None else ()
    if not groups:
        orders = overview.total_order_count if overview is not None else 0
        return _check("capital_execution", "组合执行", "ok", f"无非终态组 · 累计订单 {orders}")
    overdue = any(
        (now - item.updated_at).total_seconds() > item.maximum_unhedged_seconds
        and item.unhedged_notional > 0
        for item in groups
    )
    return _check(
        "capital_execution",
        "组合执行",
        "bad" if overdue else "warn",
        f"{len(groups)} 个非终态 ExecutionGroup",
    )


def _capital_performance_check(
    overview: CapitalOverview | None,
) -> dict:
    account = overview.account if overview is not None else None
    if account is None:
        return _check("capital_performance", "资本绩效", "unknown", "等待账户基线")
    revision = int(getattr(account, "revision", 0))
    count = overview.performance_interval_count if overview is not None else 0
    latest = overview.latest_performance if overview is not None else None
    if revision > 0 and count == 0:
        return _check(
            "capital_performance",
            "资本绩效",
            "unknown",
            "等待绩效账本启用后的下一个账户快照",
        )
    if count != revision:
        return _check(
            "capital_performance",
            "资本绩效",
            "bad",
            f"绩效区间 {count} 与账户 revision {revision} 不一致",
        )
    if latest is not None and latest.end_snapshot_id != account.snapshot_id:
        return _check(
            "capital_performance",
            "资本绩效",
            "bad",
            f"最新账户 revision {revision} 尚无匹配绩效区间",
        )
    return _check(
        "capital_performance",
        "资本绩效",
        "ok",
        f"{count} 个费用后净权益区间 · 累计 {overview.cumulative_net_pnl}",
    )


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
    market_policy = getattr(config, "market_data", None)
    perpetual_enabled = bool(
        getattr(market_policy, "perpetual_instruments", ())
    )
    perpetual_age = None
    if perpetual_enabled:
        perpetual_observed_at = reader.latest_perpetual_observed_at()
        if perpetual_observed_at is None:
            return _check(
                "data_freshness",
                "数据新鲜度",
                "unknown",
                "永续市场状态或可成交报价尚未就绪",
            )
        perpetual_age = (now - perpetual_observed_at).total_seconds()
    if market_age < 0 or account_age < 0 or (
        perpetual_age is not None and perpetual_age < 0
    ):
        return _check("data_freshness", "数据新鲜度", "bad", "观测时间晚于当前时间")
    market_limit = config.risk.maximum_market_age_seconds
    account_limit = config.risk.maximum_account_age_seconds
    perpetual_limit = (
        market_policy.perpetual_poll_seconds * 3 if perpetual_enabled else None
    )
    stale = (
        market_age > market_limit
        or account_age > account_limit
        or (
            perpetual_age is not None
            and perpetual_limit is not None
            and perpetual_age > perpetual_limit
        )
    )
    detail = f"行情 {int(market_age)}/{market_limit} 秒"
    if perpetual_age is not None and perpetual_limit is not None:
        detail += f" · 永续 {int(perpetual_age)}/{perpetual_limit} 秒"
    detail += f" · 账户 {int(account_age)}/{account_limit} 秒"
    return _check(
        "data_freshness",
        "数据新鲜度",
        "bad" if stale else "ok",
        detail,
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
    scope_states: list[tuple[int, str, float | None, int | None, str]] = []
    for scope in status.scopes:
        if scope.latest_success_at is None or scope.heartbeat_seconds is None:
            scope_states.append((1, "unknown", None, None, scope.symbol))
            continue
        age = (now - scope.latest_success_at).total_seconds()
        expected = scope.heartbeat_seconds + config.shadow.analysis_deadline_seconds
        if age < 0 or age > expected * 2:
            scope_states.append((3, "bad", age, expected, scope.symbol))
        elif age > expected:
            scope_states.append((2, "warn", age, expected, scope.symbol))
        else:
            scope_states.append((0, "ok", age, expected, scope.symbol))
    if not scope_states:
        return _check("ai_analysis", "AI 分析", "unknown", "当前版本缺少分析作用域")
    worst = max(scope_states, key=lambda item: item[0])
    _, state, age, expected, symbol = worst
    if (
        _SEVERITY[state] < _SEVERITY["warn"]
        and status.recent_attempts >= 3
        and status.recent_successes < status.recent_attempts
    ):
        state = "warn"
    if age is None or expected is None:
        detail = f"{symbol} 等待当前版本首次分析或 TriggerPlan"
    elif age < 0:
        detail = f"{symbol} 完成时间晚于当前时间"
    else:
        detail = f"最久 {symbol} {int(age)}/{expected} 秒"
    return _check(
        "ai_analysis",
        "AI 分析",
        state,
        f"{detail} · 近 1h {status.recent_successes}/{status.recent_attempts} 成功",
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


def _trigger_coordinator_check(statuses: tuple[dict, ...], now: datetime) -> dict:
    if not statuses or any("error" in item for item in statuses):
        return _check(
            "trigger_coordinator",
            "触发协调器",
            "bad",
            "Temporal 状态不可用",
        )
    pending = sum(int(item.get("pending_count", 0)) for item in statuses)
    active = sum(item.get("active_batch_id") is not None for item in statuses)
    overdue_or_unknown = 0
    for item in statuses:
        count = int(item.get("pending_count", 0))
        if count <= 0 or item.get("active_batch_id") is not None:
            continue
        raw_reconsider_at = item.get("next_reconsider_at")
        try:
            reconsider_at = (
                datetime.fromisoformat(raw_reconsider_at)
                if isinstance(raw_reconsider_at, str)
                else None
            )
        except ValueError:
            reconsider_at = None
        if reconsider_at is None or reconsider_at.tzinfo is None or reconsider_at <= now:
            overdue_or_unknown += count
    if overdue_or_unknown:
        return _check(
            "trigger_coordinator",
            "触发协调器",
            "warn",
            f"到期未执行或状态不明 {overdue_or_unknown} 条 · 执行中 {active} 批",
        )
    if pending:
        return _check(
            "trigger_coordinator",
            "触发协调器",
            "ok",
            f"正常等待 {pending} 条 · 执行中 {active} 批",
        )
    return _check(
        "trigger_coordinator",
        "触发协调器",
        "ok",
        f"无积压 · 执行中 {active} 批",
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
