"""投影层：把领域对象组装成前端直接消费的 plain DTO（JSON 安全）。

Decimal 一律转字符串保精度，datetime 转 ISO8601。措辞与摘要拼装委托 ``formatting``。
权益曲线是对同一账户全部 Pipeline 的 ``DecisionOutcome.net_pnl`` 按平仓时间累加；聚合
指标与治理评估器复用同一纯函数，前端一律不重算。
"""

from __future__ import annotations

from decimal import Decimal

from investment_manager.dashboard import formatting as fmt
from investment_manager.dashboard.read_models import (
    AccountStatus,
    CycleRow,
    EquityWindow,
    WorldEvent,
)
from investment_manager.domain import (
    AnalysisProposal,
    MetricObservation,
    PanelSnapshot,
    TradeIntent,
)
from investment_manager.execution.ledger import CycleFacts
from investment_manager.execution.lifecycle import OpenLifecycleRecord
from investment_manager.execution.models import Order
from investment_manager.execution.reconciliation import ReconciliationReport
from investment_manager.risk.models import RiskDecision

_ECONOMICS_METRICS = (
    "expected_net_edge_bps",
    "remaining_gross_edge_bps",
    "price_move_consumed_bps",
    "signal_age_seconds",
)


def cycle_row(row: CycleRow) -> dict:
    return {
        "cycle_id": row.cycle_id,
        "at": fmt.iso(row.as_of),
        "symbol": row.symbol,
        "outcome": row.outcome,
        "category": fmt.CATEGORY.get(row.outcome, "no-action"),
        "pill": fmt.PILL_LABEL.get(row.outcome, row.outcome),
        "summary": fmt.compose_summary(
            outcome=row.outcome, reason_code=row.reason_code, intent=row.intent
        ),
        "reason": fmt.thesis_gist(row.proposal),
        "confidence": _confidence(row.proposal),
    }


def cycle_detail(facts: CycleFacts) -> dict:
    return {
        "cycle_id": facts.cycle_id,
        "outcome": facts.outcome,
        "reason_code": facts.reason_code,
        "category": fmt.CATEGORY.get(facts.outcome, "no-action"),
        "rail": _rail(facts),
        "ai": _ai_view(facts.analysis_proposal),
        "economics": _economics(facts.metrics),
        "risk_checks": _risk_checks(facts.risk_decision),
        "action": _action(facts.intent, facts.order),
        "snapshot": snapshot(facts.panel),
    }


def snapshot(panel: PanelSnapshot) -> dict:
    market = panel.market
    features = panel.features
    account = panel.account
    return {
        "symbol": panel.symbol,
        "as_of": fmt.iso(panel.as_of),
        "policy_version": panel.policy_version,
        "content_hash": panel.content_hash,
        "data_quality": list(panel.data_quality),
        "market": {
            "last": fmt.money(market.last),
            "bid": fmt.money(market.bid),
            "ask": fmt.money(market.ask),
            "source": market.source,
        },
        "features": {
            "regime": features.regime,
            "return_fraction": fmt.money(features.return_fraction),
            "realized_volatility": fmt.money(features.realized_volatility),
            "atr": fmt.money(features.atr),
            "spread_bps": fmt.money(features.spread_bps),
            "volume_ratio": fmt.money(features.volume_ratio),
            "market_age_seconds": features.market_age_seconds,
        },
        "account": {
            "quote_balance": fmt.money(account.quote_balance),
            "position_count": len(account.positions),
            "positions": [
                {
                    "symbol": position.symbol,
                    "quantity": fmt.money(position.quantity),
                    "average_price": fmt.money(position.average_price),
                }
                for position in account.positions
            ],
            "open_order_count": account.open_order_count,
            "daily_pnl": fmt.money(account.daily_pnl),
            "drawdown_fraction": fmt.money(account.drawdown_fraction),
            "reconciled": account.reconciled,
        },
        "evidence": [
            {
                "source": item.source,
                "title": item.title,
                "excerpt": item.excerpt,
                "value_score": fmt.money(item.value_score),
                "injection_suspected": item.prompt_injection_suspected,
            }
            for item in panel.evidence
        ],
        "rules": list(panel.rules_digest),
    }


def position(record: OpenLifecycleRecord, *, mark: Decimal | None, side: str | None) -> dict:
    lifecycle = record.lifecycle
    unrealized = _unrealized(lifecycle, mark, side)
    return {
        "position_id": lifecycle.position_id,
        "symbol": lifecycle.symbol,
        "direction": _direction(side),
        "quantity": fmt.money(lifecycle.quantity),
        "entry_price": fmt.money(lifecycle.entry_price),
        "stop_price": fmt.money(lifecycle.stop_price),
        "mark_price": fmt.money(mark),
        "unrealized_estimate": fmt.money(unrealized),
        "status": lifecycle.status,
        "opened_at": fmt.iso(lifecycle.opened_at),
        "max_exit_at": fmt.iso(lifecycle.max_exit_at),
    }


def world_event(event: WorldEvent) -> dict:
    return {
        "kind": event.kind,
        "at": fmt.iso(event.at),
        "source": event.source,
        "title": event.title,
        "symbols": list(event.symbols),
        "impact": event.impact,
        "injection_suspected": event.injection_suspected,
        "fed_cycle_id": event.fed_cycle_id,
        "fed_cycle_at": fmt.iso(event.fed_cycle_at),
    }


def equity(window: EquityWindow) -> dict:
    ordered = window.outcomes
    curve: list[dict] = []
    running = Decimal("0")
    for outcome in ordered:
        running += outcome.net_pnl
        curve.append({"at": fmt.iso(outcome.closed_at), "equity": fmt.money(running)})
    return {
        "lookback_start": fmt.iso(window.lookback_start),
        "lookback_end": fmt.iso(window.lookback_end),
        "trade_count": len(ordered),
        "curve": curve,
        "summary": _outcome_summary(window),
    }


def account_status(status: AccountStatus) -> dict:
    return {
        "account_id": status.account_id,
        "enabled": status.enabled,
        "state": _account_state(status),
        "headroom_percent": status.headroom_percent,
        "healthy": status.healthy,
        "observed_at": fmt.iso(status.observed_at),
        "recent_failures": status.recent_failures,
    }


def reconciliation(report: ReconciliationReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "status": report.status,
        "freeze_new_risk": report.freeze_new_risk,
        "as_of": fmt.iso(report.as_of),
        "difference_count": len(report.differences),
        "differences": [
            {
                "kind": diff.kind,
                "key": diff.key,
                "local": diff.local_value,
                "remote": diff.remote_value,
            }
            for diff in report.differences
        ],
    }


# --- internals -----------------------------------------------------------
def _confidence(proposal: AnalysisProposal | None) -> float | None:
    return None if proposal is None else float(proposal.confidence)


def _ai_view(proposal: AnalysisProposal | None) -> dict | None:
    if proposal is None:
        return None
    return {
        "suggested_action": proposal.suggested_action,
        "side": proposal.side,
        "thesis": proposal.thesis,
        "confidence": float(proposal.confidence),
        "unknowns": list(proposal.unknowns),
    }


def _economics(metrics: tuple[MetricObservation, ...]) -> dict | None:
    indexed = _metrics_by_name(metrics)
    if not any(name in indexed for name in _ECONOMICS_METRICS):
        return None
    return {name: fmt.money(indexed.get(name)) for name in _ECONOMICS_METRICS}


def _risk_checks(risk: RiskDecision | None) -> list[dict]:
    if risk is None:
        return []
    return [
        {
            "rule": fmt.rule_plain(result.rule_id),
            "state": result.state,
            "observed": result.observed,
            "limit": result.limit,
        }
        for result in risk.rule_results
    ]


def _action(intent: TradeIntent | None, order: Order | None) -> dict | None:
    if intent is None:
        return None
    return {
        "direction": fmt.direction_label(intent.side),
        "entry": "市价" if intent.entry.price is None else fmt.money(intent.entry.price),
        "stop": fmt.money(intent.stop_price),
        "max_holding_minutes": intent.max_holding_minutes,
        "order_status": None if order is None else order.status,
        "filled_quantity": _filled_quantity(order),
    }


def _rail(facts: CycleFacts) -> dict:
    """后端给出完整门禁状态，前端只渲染，避免两端重复推断业务语义。"""

    risk_approved = facts.risk_decision is not None and facts.risk_decision.outcome == "APPROVED"
    stop_key = _rail_stop_key(facts)
    codex_attempted = any(
        dict(item.dimensions).get("metric") == "codex_analysis_success" for item in facts.metrics
    )
    gates = [
        ("panel", "面板就绪", "pass", ""),
        (
            "proposal",
            "AI 建议",
            "soft" if facts.analysis_proposal is not None else "skip",
            "软 · 建议"
            if facts.analysis_proposal is not None
            else "分析失败"
            if codex_attempted
            else "未启用",
        ),
        ("candidate", "生成候选", "pass" if facts.candidates else "skip", ""),
        ("intent", "合成意图", "pass" if facts.intent is not None else "skip", ""),
        (
            "frequency",
            "频率与成本",
            "pass" if facts.risk_decision is not None else "skip",
            "",
        ),
        (
            "risk",
            "风控",
            "pass" if risk_approved else "skip",
            "",
        ),
        (
            "execution",
            "下单执行",
            "pass"
            if facts.order is not None and facts.order.fills
            else "pending"
            if facts.outcome == "EXECUTION_PENDING"
            else "skip",
            "等待执行" if facts.outcome == "EXECUTION_PENDING" else "",
        ),
        ("position", "建立持仓", "pass" if facts.position_lifecycle is not None else "skip", ""),
        ("outcome", "结算", "pass" if facts.decision_outcome is not None else "skip", ""),
    ]
    stop_notes = {
        "proposal": "分析或校验失败",
        "candidate": "没有形成候选",
        "intent": "无法合成意图",
        "frequency": "优势或频率不足",
        "risk": "风控或风险预算拒绝",
        "execution": "订单未成交",
    }
    if stop_key is not None:
        stop_index = next(index for index, gate in enumerate(gates) if gate[0] == stop_key)
        gates = [
            (
                key,
                label,
                "stop" if index == stop_index else "skip" if index > stop_index else state,
                stop_notes[stop_key] if index == stop_index else "" if index > stop_index else note,
            )
            for index, (key, label, state, note) in enumerate(gates)
        ]
    return {
        "gates": [
            {"key": key, "label": label, "state": state, "note": note}
            for key, label, state, note in gates
        ]
    }


def _rail_stop_key(facts: CycleFacts) -> str | None:
    if facts.reason_code.startswith("CODEX_"):
        return "proposal"
    if facts.outcome == "NO_ACTION":
        return "candidate" if not facts.candidates else "intent"
    if facts.outcome == "RISK_REJECTED":
        return "risk"
    if facts.outcome != "NO_TRADE":
        return None
    if facts.intent is not None and facts.risk_decision is None:
        return "frequency"
    if facts.risk_decision is not None and facts.execution_request is None:
        return "risk"
    if facts.order is not None and not facts.order.fills:
        return "execution"
    return "frequency"


def _outcome_summary(window: EquityWindow) -> dict:
    report = window.metrics
    return {
        "window_start": fmt.iso(window.lookback_start),
        "window_end": fmt.iso(window.lookback_end),
        "net_pnl": fmt.money(report.net_pnl),
        "total_fees": fmt.money(report.total_fees),
        "win_rate": fmt.money(report.win_rate),
        "profit_factor": fmt.money(report.profit_factor),
        "maximum_drawdown": fmt.money(report.maximum_drawdown),
        "closed_trade_count": report.closed_trade_count,
    }


def _unrealized(lifecycle, mark: Decimal | None, side: str | None) -> Decimal | None:
    if mark is None:
        return None
    move = mark - lifecycle.entry_price
    if side == "SELL":  # 空头方向相反；未知方向按多头估算（当前 MVP 只做多）
        move = -move
    return move * lifecycle.quantity


def _direction(side: str | None) -> str | None:
    if side == "BUY":
        return "多"
    if side == "SELL":
        return "空"
    return None


def _filled_quantity(order: Order | None) -> str | None:
    if order is None or not order.fills:
        return None
    total = sum((fill.quantity for fill in order.fills), Decimal("0"))
    return fmt.money(total)


def _account_state(status: AccountStatus) -> str:
    if not status.enabled:
        return "DISABLED"
    if status.healthy is False:
        return "COOLDOWN"
    if status.leased:
        return "LEASED"
    if status.healthy is True:
        return "HEALTHY"
    # 容量只在真实分析调用前探测，且探测结果很快过期。此时账号的启用状态
    # 是确定事实，未知的只是瞬时健康度；把两者都显示成 UNKNOWN 会误导运维。
    return "ENABLED"


def _metrics_by_name(metrics: tuple[MetricObservation, ...]) -> dict[str, Decimal]:
    indexed: dict[str, Decimal] = {}
    for observation in metrics:
        name = dict(observation.dimensions).get("metric")
        if name is not None:
            indexed[name] = observation.value
    return indexed
