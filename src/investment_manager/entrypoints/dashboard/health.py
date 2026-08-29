"""单一健康状态（异常驱动）：把散落的守卫合成一个结论 + 明细。

正常时前端只显示「运行正常」，异常才点名。缺数据时返回 ``unknown``（失败关闭：不假装
正常，也不误报为红）。数据新鲜度直接读取实时行情投影，账户新鲜度复用同一次对账查询。
"""

from __future__ import annotations

from datetime import datetime

from investment_manager.entrypoints.dashboard.capital import CapitalOverview
from investment_manager.entrypoints.dashboard.formatting import (
    assessment_reason_plain,
    display_decimal,
)
from investment_manager.entrypoints.dashboard.read_models import (
    AnalysisRuntimeStatus,
    AssessmentQualityStatus,
    DashboardReader,
)
from investment_manager.forecast.context.increment_evidence import (
    ForecastIncrementEvidence,
)

_SEVERITY = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}
_DISK_WARN_PERCENT = 90
_DISK_BAD_PERCENT = 95


def assemble_health(
    reader: DashboardReader,
    config,
    *,
    now: datetime,
    capital_overview: CapitalOverview | None = None,
    assessment_quality: AssessmentQualityStatus | None = None,
    forecast_research: ForecastIncrementEvidence | None = None,
    host_resources: dict | None = None,
    coordinator_statuses: tuple[dict, ...] | None = None,
) -> dict:
    analysis = reader.analysis_runtime_status(now=now)
    checks = [
        _capital_account_check(capital_overview, config),
        _capital_freshness_check(reader, capital_overview, analysis, config, now),
        _capital_decision_check(capital_overview, now),
        _capital_execution_check(capital_overview, now),
        _capital_performance_check(capital_overview),
        _trigger_delivery_check(analysis, config, now),
        _release_alignment_check(analysis),
    ]
    if assessment_quality is not None:
        checks.append(_assessment_output_quality_check(assessment_quality))
    if forecast_research is not None:
        checks.append(_forecast_research_check(forecast_research))
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


def _forecast_research_check(evidence: ForecastIncrementEvidence) -> dict:
    if evidence.candidate_behavior_id is None:
        return _check(
            "forecast_research",
            "前向预测",
            "unknown",
            "现役预测行为尚未登记",
        )
    if evidence.pending_panel_count:
        return _check(
            "forecast_research",
            "前向预测",
            "warn",
            f"{evidence.pending_panel_count} 个到期时点仍在分析",
        )
    if evidence.unavailable_panel_count:
        return _check(
            "forecast_research",
            "前向预测",
            "warn",
            f"{evidence.unavailable_panel_count} 个到期时点未形成预测",
        )
    if evidence.due_panel_count == 0:
        return _check(
            "forecast_research",
            "前向预测",
            "ok",
            "现役先验与世界认知预测已登记 · 尚无到期时点",
        )
    return _check(
        "forecast_research",
        "前向预测",
        "ok",
        f"{evidence.forecast_panel_count} 个到期时点已形成预测 · "
        f"{evidence.settled_panel_count} 个已结算",
    )


def _assessment_output_quality_check(status: AssessmentQualityStatus) -> dict:
    rejected = status.rejected_attempt_count_24h
    latest = status.latest_attempt_status
    if latest in {"REJECTED", "FAILED"}:
        state = "bad" if status.latest_valid_at is None else "warn"
        detail = "最近一次模型调用未产生可持久化结果"
        if status.latest_attempt_reason:
            detail += f"：{assessment_reason_plain(status.latest_attempt_reason)}"
    elif latest == "NO_ATTEMPT":
        state = "unknown"
        detail = "当前世界认知行为版本尚无分析尝试"
    else:
        state = "ok"
        if status.execution_count_24h:
            detail = (
                "最近一次输出有效 · 过去 24 小时成功 "
                f"{status.final_success_count_24h}/{status.execution_count_24h} 次"
            )
            if rejected:
                detail += f" · 拒绝 {rejected} 次"
        else:
            detail = "最近一次输出有效"
    return _check("ai_output_quality", "世界认知 AI", state, detail)


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
            f"回撤 {display_decimal(account.drawdown_fraction * 100)}% 超过限制",
        )
    return _check(
        "capital_account",
        "资本账户",
        "ok",
        f"权益 {display_decimal(account.equity)} {account.settlement_asset} · "
        f"现金 {display_decimal(account.cash_balance)}",
    )


def _capital_freshness_check(
    reader: DashboardReader,
    overview: CapitalOverview | None,
    analysis: AnalysisRuntimeStatus,
    config,
    now: datetime,
) -> dict:
    spot_at = reader.latest_market_observed_at()
    account = overview.account if overview is not None else None
    products = {
        item.instrument.product.value for item in config.capital.execution_specs
    }
    perpetual_at = (
        reader.latest_perpetual_observed_at()
        if any(product != "SPOT" for product in products)
        else None
    )
    owner_symbol = config.assessment.review_trigger_symbol
    owner_scope = next(
        (item for item in analysis.scopes if item.symbol == owner_symbol),
        None,
    )
    if (
        spot_at is None
        or account is None
        or owner_scope is None
        or owner_scope.heartbeat_seconds is None
        or (perpetual_at is None and any(product != "SPOT" for product in products))
    ):
        return _check("capital_freshness", "资本事实新鲜度", "unknown", "事实尚未齐备")
    ages = [
        (
            "Spot",
            (now - spot_at).total_seconds(),
            config.capital.risk.maximum_quote_age_seconds,
        )
    ]
    if perpetual_at is not None:
        ages.append(
            (
                "Perpetual",
                (now - perpetual_at).total_seconds(),
                config.capital.risk.maximum_quote_age_seconds,
            )
        )
    account_limit = (
        owner_scope.heartbeat_seconds
        + config.shadow.analysis_deadline_seconds
    )
    ages.append(
        ("账户", (now - account.as_of).total_seconds(), account_limit)
    )
    if any(age < 0 for _, age, _ in ages):
        return _check("capital_freshness", "资本事实新鲜度", "bad", "事实时间晚于当前时间")
    stale = any(age > limit for _, age, limit in ages)
    origin = owner_scope.trigger_plan_origin or "UNKNOWN"
    revision = owner_scope.trigger_plan_revision
    return _check(
        "capital_freshness",
        "资本事实新鲜度",
        "bad" if stale else "ok",
        " · ".join(f"{label} {int(age)}/{limit} 秒" for label, age, limit in ages)
        + f" · TriggerPlan r{revision or '?'} {origin}",
    )


def _capital_decision_check(
    overview: CapitalOverview | None,
    now: datetime,
) -> dict:
    target = overview.target if overview is not None else None
    account = overview.account if overview is not None else None
    record = overview.cycle_record if overview is not None else None
    if account is None:
        return _check("capital_decision", "资本决策", "unknown", "等待首次评估")
    if target is None:
        if record is None:
            if not getattr(account, "sleeves", ()):
                return _check(
                    "capital_decision",
                    "资本决策",
                    "ok",
                    "当前全现金 · 无待执行资本动作",
                )
            return _check("capital_decision", "资本决策", "unknown", "等待首次决策记录")
        return _check(
            "capital_decision",
            "资本决策",
            "ok",
            ", ".join(record.reason_codes),
        )
    age = (now - target.as_of).total_seconds()
    if age < 0:
        return _check("capital_decision", "资本决策", "bad", "决策时间晚于当前时间")
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
        f"{count} 个费用后净权益区间 · 累计 "
        f"{display_decimal(overview.cumulative_net_pnl)}",
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
    unresolved_failures = sum(
        item.get("unresolved_failure") is True for item in statuses
    )
    if unresolved_failures:
        return _check(
            "trigger_coordinator",
            "触发协调器",
            "bad",
            f"{unresolved_failures} 个协调器最近一批处理失败，等待成功复核",
        )
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


def _check(key: str, name: str, state: str, detail: str) -> dict:
    return {"key": key, "name": name, "state": state, "detail": detail}
