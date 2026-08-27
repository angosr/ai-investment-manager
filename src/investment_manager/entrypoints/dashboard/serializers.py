"""投影层：把领域对象组装成前端直接消费的 plain DTO（JSON 安全）。

Decimal 一律转字符串保精度，datetime 转 ISO8601。措辞与摘要拼装委托 ``formatting``。
"""

from __future__ import annotations

from investment_manager.entrypoints.dashboard import formatting as fmt
from investment_manager.entrypoints.dashboard.read_models import (
    AccountStatus,
    AssessmentQualityStatus,
    AssessmentRecord,
    TokenUsageDay,
    TokenUsageWindow,
    WorldEvent,
)
from investment_manager.forecast.context.contract import assessment_input_projection
from investment_manager.state.decision.packet import DecisionPacket


def assessment_row(record: AssessmentRecord) -> dict:
    assessment = record.assessment
    cited_evidence_ids = set(_assessment_cited_ids(assessment))
    return {
        "schema_version": assessment.schema_version,
        "assessment_id": assessment.assessment_id,
        "at": fmt.iso(assessment.available_at),
        "scope": assessment.analysis_scope,
        "summary": _assessment_summary(assessment),
        "synthesis": assessment.synthesis,
        "synthesis_horizon_hours": assessment.synthesis_horizon_hours,
        "driver_count": len(assessment.mechanisms),
        "evidence_count": len(cited_evidence_ids),
    }


def assessment_detail(
    record: AssessmentRecord,
    *,
    observations=(),
) -> dict:
    assessment = record.assessment
    evidence_catalog = _assessment_evidence_catalog(record.packet)
    cited_ids = _assessment_cited_ids(assessment)
    from investment_manager.forecast.context.verification import verification_test_id

    latest_observation_by_test = {
        item.test_id: item
        for item in sorted(
            observations,
            key=lambda value: (value.observed_at, value.observation_id),
        )
    }
    return {
        **assessment_row(record),
        "as_of": fmt.iso(assessment.as_of),
        "mechanisms": [
            {
                "mechanism_id": mechanism.mechanism_id,
                "continuity_ref": mechanism.continuity_ref,
                "relationship": mechanism.relationship.value,
                "claim": mechanism.claim,
                "horizon_hours": mechanism.horizon_hours,
                "transmission_stage": mechanism.transmission_stage.value,
                "causal_chain": [
                    {
                        "statement": node.statement,
                        "evidence": _resolve_assessment_evidence(
                            node.evidence_ids,
                            evidence_catalog,
                        ),
                    }
                    for node in mechanism.causal_chain
                ],
                "conflicting_evidence": _resolve_assessment_evidence(
                    mechanism.conflicting_evidence_ids,
                    evidence_catalog,
                ),
                "verification_tests": [
                    {
                        **test.model_dump(mode="json"),
                        "latest_observation": (
                            observation.model_dump(mode="json")
                            if (
                                observation := latest_observation_by_test.get(
                                    verification_test_id(
                                        assessment_id=assessment.assessment_id,
                                        mechanism_id=mechanism.mechanism_id,
                                        test_index=index,
                                        test=test,
                                    )
                                )
                            )
                            is not None
                            else None
                        ),
                    }
                    for index, test in enumerate(mechanism.verification_tests)
                ],
                "invalidation_conditions": list(mechanism.invalidation_conditions),
                "next_review_at": fmt.iso(mechanism.next_review_at),
            }
            for mechanism in assessment.mechanisms
        ],
        "retired_mechanisms": [
            {
                "previous_mechanism_id": item.previous_mechanism_id,
                "rationale": item.rationale,
                "evidence": _resolve_assessment_evidence(
                    item.evidence_ids,
                    evidence_catalog,
                ),
            }
            for item in assessment.retired_mechanisms
        ],
        "event_references": [
            {
                "evidence_id": item.evidence_id,
                "source": item.source,
                "title": item.title,
                "event_time": fmt.iso(item.event_time),
                "impact_state": item.impact_state.value,
                "rationale": item.rationale,
                "stale_at": fmt.iso(item.stale_at),
            }
            for item in assessment.event_references
        ],
        "cited_evidence": _resolve_assessment_evidence(
            cited_ids,
            evidence_catalog,
        ),
        "input_snapshot": (
            None if record.packet is None else _assessment_input_snapshot(record.packet)
        ),
    }


def _assessment_evidence_catalog(packet: DecisionPacket | None) -> dict[str, dict]:
    if packet is None:
        return {}
    catalog: dict[str, dict] = {}
    for fact in packet.facts:
        catalog[fact.revision_id] = {
            "evidence_id": fact.revision_id,
            "kind": (
                "FIRST_PARTY_FACT"
                if fact.highest_source_tier.value == "FIRST_PARTY"
                else "STRUCTURED_FACT"
            ),
            "title": fact.headline,
            "detail": fact.claim,
            "source": fact.highest_source_tier.value,
            "at": fmt.iso(fact.event_time or fact.observed_at),
        }
    for event in packet.intelligence_events:
        catalog[event.evidence_ref] = {
            "evidence_id": event.evidence_ref,
            "kind": "INTELLIGENCE_EVENT",
            "title": event.title,
            "detail": event.body,
            "source": event.source,
            "at": fmt.iso(event.event_time),
        }
    for state in packet.derivative_states:
        details = (
            ("永续溢价", state.mark_index_premium_bps, "bps"),
            ("可执行空头基差", state.executable_short_basis_bps, "bps"),
            ("最近资金费率", state.last_funding_rate_bps, "bps"),
            ("窗口资金费率均值", getattr(state, "trailing_funding_rate_mean_bps", None), "bps"),
            ("窗口资金费率波动", getattr(state, "trailing_funding_rate_stddev_bps", None), "bps"),
            (
                "窗口资金费率为正占比",
                getattr(state, "trailing_funding_positive_fraction", None),
                "",
            ),
            ("窗口资金费率最低值", getattr(state, "trailing_funding_rate_min_bps", None), "bps"),
            ("现货主动买卖比", state.spot_taker_buy_sell_ratio, ""),
            ("OI 变化", state.open_interest_change_fraction, ""),
            ("多头账户占比", state.global_long_account_fraction, ""),
            ("永续主动买卖比", state.taker_buy_sell_ratio, ""),
        )
        catalog[state.evidence_ref] = {
            "evidence_id": state.evidence_ref,
            "kind": "MARKET_STRUCTURE",
            "title": f"{state.asset} 现货与衍生品结构",
            "detail": "；".join(
                f"{label} {value}{unit}" for label, value, unit in details if value is not None
            ),
            "source": "BINANCE_MARKET",
            "at": fmt.iso(state.observed_at),
        }
    for delta in packet.deltas:
        catalog[delta.delta_id] = {
            "evidence_id": delta.delta_id,
            "kind": "MATERIAL_DELTA",
            "title": " / ".join(delta.reason_codes),
            "detail": "、".join((*delta.affected_assets, *delta.risk_factors)),
            "source": delta.category.value,
            "at": fmt.iso(delta.observed_at),
        }
        for feature_ref in delta.feature_snapshot_refs:
            catalog[feature_ref] = {
                "evidence_id": feature_ref,
                "kind": "MARKET_FEATURE",
                "title": "市场特征发生材料变化",
                "detail": "、".join(delta.affected_assets),
                "source": "MARKET",
                "at": fmt.iso(delta.observed_at),
            }
    if packet.previous_context is not None:
        previous = packet.previous_context
        catalog[previous.assessment_id] = {
            "evidence_id": previous.assessment_id,
            "kind": "PREVIOUS_CONTEXT",
            "title": "上一轮世界认知",
            "detail": previous.synthesis,
            "source": "CONTEXT_ASSESSMENT",
            "at": fmt.iso(previous.available_at),
        }
        for item in previous.event_references:
            catalog[item.evidence_id] = {
                "evidence_id": item.evidence_id,
                "kind": "INTELLIGENCE_EVENT",
                "title": item.title,
                "detail": item.rationale,
                "source": item.source,
                "at": fmt.iso(item.event_time),
            }
    return catalog


def _resolve_assessment_evidence(
    evidence_ids: tuple[str, ...],
    catalog: dict[str, dict],
) -> list[dict]:
    return [catalog[item] for item in evidence_ids if item in catalog]


def assessment_quality(status: AssessmentQualityStatus) -> dict:
    return {
        "latest_attempt_at": fmt.iso(status.latest_attempt_at),
        "latest_attempt_status": status.latest_attempt_status,
        "latest_attempt_reason": status.latest_attempt_reason,
        "latest_valid_at": fmt.iso(status.latest_valid_at),
        "rejected_attempt_count_24h": status.rejected_attempt_count_24h,
        "execution_count_24h": status.execution_count_24h,
        "final_success_count_24h": status.final_success_count_24h,
        "first_attempt_success_count_24h": status.first_attempt_success_count_24h,
        "rejection_reasons": [
            fmt.assessment_reason_plain(code) for code in status.rejection_reason_codes
        ],
    }


def _assessment_input_snapshot(packet: DecisionPacket) -> dict:
    """Return the exact persisted AI input without inventing a second state model."""

    return assessment_input_projection(packet)


def world_event(event: WorldEvent) -> dict:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "at": fmt.iso(event.at),
        "source": event.source,
        "title": event.title,
        "symbols": list(event.symbols),
        "attention_priority": event.attention_priority,
        "priority": event.priority,
        "injection_suspected": event.injection_suspected,
        "fed_cycle_id": event.fed_cycle_id,
        "fed_cycle_at": fmt.iso(event.fed_cycle_at),
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


def token_usage(window: TokenUsageWindow) -> dict:
    def daily(points: tuple[TokenUsageDay, ...]) -> list[dict]:
        return [
            {"date": point.date.isoformat(), "total_tokens": point.total_tokens}
            for point in points
        ]

    return {
        "window_days": window.window_days,
        "start_date": window.start_date.isoformat(),
        "end_date": window.end_date.isoformat(),
        "total_tokens": window.total_tokens,
        "daily": daily(window.daily),
        "accounts": [
            {
                "account_id": account.account_id,
                "total_tokens": account.total_tokens,
                "daily": daily(account.daily),
            }
            for account in window.accounts
        ],
    }


# --- internals -----------------------------------------------------------
def _assessment_summary(assessment) -> str:
    """Use the highest-decision-value mechanism as the compact conclusion."""
    return assessment.mechanisms[0].claim


def _assessment_cited_ids(assessment) -> tuple[str, ...]:
    ids: list[str] = []
    for mechanism in assessment.mechanisms:
        for node in mechanism.causal_chain:
            ids.extend(node.evidence_ids)
        ids.extend(mechanism.conflicting_evidence_ids)
    return tuple(dict.fromkeys(ids))


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
