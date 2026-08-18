"""纯格式化与措辞：数字、时间、方向、reason_code → 运行者能懂的中文。

这里只做「事实 → 说法」的确定性映射，无 IO、无副作用，便于单元测试。决策记录每行
「一句人话」的拼装规则（设计文档 §5.3）集中在这里，前端只渲染成句、不再拼措辞。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quant_core.domain import AnalysisProposal, Side, TradeIntent

# 周期结果 → 时间线行的类别（决定色条与徽章样式）与徽章中文
CATEGORY = {
    "EXECUTED": "exec",
    "EXECUTION_PENDING": "pending",
    "RISK_REJECTED": "rejected",
    "NO_TRADE": "no-trade",
    "NO_ACTION": "no-action",
}
PILL_LABEL = {
    "EXECUTED": "已成交",
    "EXECUTION_PENDING": "下单中",
    "RISK_REJECTED": "风控拒绝",
    "NO_TRADE": "未交易",
    "NO_ACTION": "未行动",
}

# reason_code → 中文短语；未收录的原样回退，保证不吞信息
REASON_PLAIN = {
    "PORTFOLIO_RISK_BUDGET_EXHAUSTED": "组合风险超限",
    "INSUFFICIENT_NET_EDGE": "扣掉成本后优势不足",
    "DAILY_ORDER_BUDGET_EXHAUSTED": "当日下单次数已用完",
    "SYMBOL_COOLDOWN_ACTIVE": "该品种处于冷却期",
    "INTENT_EXPIRED": "信号已过期",
    "ALPHA_ALREADY_CONSUMED": "优势已被价格吃掉",
    "NO_VALID_CANDIDATE": "没有形成有效候选",
    "CODEX_ACCOUNTS_UNAVAILABLE": "AI 账号不可用",
    "CODEX_RATE_LIMIT": "AI 账号额度受限",
}

# 风控规则 rule_id → 中文名
RULE_PLAIN = {
    "market_data_fresh": "行情数据新鲜",
    "account_data_fresh": "账户数据新鲜",
    "portfolio_risk_budget": "组合风险预算",
    "stop_coverage": "止损覆盖",
    "daily_loss_limit": "当日亏损上限",
    "position_protected": "持仓保护",
    "reconciled": "已对账",
}


def money(value: Decimal | None) -> str | None:
    """Decimal → 字符串，保留精度（前端只显示，不参与计算）。"""

    return None if value is None else str(value)


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def reason_plain(reason_code: str) -> str:
    return REASON_PLAIN.get(reason_code, reason_code)


def rule_plain(rule_id: str) -> str:
    return RULE_PLAIN.get(rule_id, rule_id)


def direction_label(side: Side | None) -> str | None:
    if side is None:
        return None
    return "多" if side == Side.BUY else "空"


def thesis_gist(proposal: AnalysisProposal | None, *, limit: int = 90) -> str | None:
    """AI thesis 的一行摘要，供决策记录第二行显示。"""

    if proposal is None or not proposal.thesis:
        return None
    text = proposal.thesis.strip().splitlines()[0]
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"AI：{text}"


def compose_summary(*, outcome: str, reason_code: str, intent: TradeIntent | None) -> str:
    """决策记录每行的「一句人话」摘要（不点开也能懂）。"""

    if outcome == "EXECUTED" and intent is not None:
        return _executed_summary(intent)
    if outcome == "EXECUTION_PENDING":
        return "下单中 · 等待成交"
    if outcome == "RISK_REJECTED":
        return f"未开仓 · 风控拒绝：{reason_plain(reason_code)}"
    if outcome == "NO_TRADE":
        return f"未开仓 · {reason_plain(reason_code)}"
    if outcome == "NO_ACTION":
        return "未行动 · 没有值得建仓的机会"
    return reason_plain(reason_code)


def _executed_summary(intent: TradeIntent) -> str:
    direction = direction_label(intent.side) or "?"
    entry = intent.entry.price
    entry_text = "市价" if entry is None else str(entry)
    return f"开{direction}仓 @ {entry_text}，止损 {intent.stop_price}"
