"""Current dashboard value, time, and assessment-status formatting."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

_ASSESSMENT_RUNTIME_REASONS = {
    "CODEX_ACCOUNTS_UNAVAILABLE": "AI 账号不可用",
    "CODEX_RATE_LIMIT": "AI 账号额度受限",
    "CODEX_ANALYST_UNAVAILABLE": "AI 分析器不可用",
    "CODEX_AUDIT_WRITE_FAILED": "AI 审计写入失败",
    "CODEX_BUNDLE_INVALID": "AI 运行包无效",
    "CODEX_PROMPT_CAPACITY_EXCEEDED": "AI 输入超过容量上限",
    "CODEX_DETERMINISTIC_VALIDATION": "AI 建议未通过确定性校验",
    "CODEX_RUNTIME_DISABLED": "AI 运行时未启用",
}

ASSESSMENT_REASON_PLAIN = {
    "SCHEMA_INVALID": "AI 输出格式不符合契约",
    "CODEX_SCHEMA_INVALID": "AI 输出格式不符合契约",
    "ASSESSMENT_VIEW_SET_INVALID": "资产与时域判断不完整",
    "ASSESSMENT_EVIDENCE_NOT_VISIBLE": "引用了输入快照之外的证据",
    "ASSESSMENT_CIRCULAR_INFERENCE": "使用上一版认知循环自证",
    "ASSESSMENT_CONFIRMED_EVIDENCE_INVALID": "把推断误标为已确认事实",
    "ASSESSMENT_EVENT_UPDATE_DUPLICATED": "事件影响更新重复",
    "ASSESSMENT_EVENT_NOT_VISIBLE": "引用了输入快照之外的事件",
    "ASSESSMENT_STALE_EVENT_REVIVED": "试图恢复已过时事件",
    "ASSESSMENT_STALE_EVENT_REFERENCED": "使用已过时事件支撑当前判断",
    "ASSESSMENT_ACTIVE_EVENT_NOT_REGISTERED": "引用事件但未维护其影响状态",
    "ASSESSMENT_NEW_EVENT_MARKED_STALE": "新事件被直接标为过时",
    "ASSESSMENT_EVENT_CONTENT_MISSING": "事件引用缺少可冻结内容",
}

def money(value: Decimal | None) -> str | None:
    """Decimal → 字符串，保留精度（前端只显示，不参与计算）。"""

    return None if value is None else str(value)


def display_decimal(value: Decimal, *, places: int = 2) -> str:
    """Compact Decimal for prose-only dashboard health details."""

    rendered = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def assessment_reason_plain(reason_code: str) -> str:
    return ASSESSMENT_REASON_PLAIN.get(
        reason_code,
        _ASSESSMENT_RUNTIME_REASONS.get(reason_code, "AI 分析运行失败"),
    )
