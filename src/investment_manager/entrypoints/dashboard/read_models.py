"""只读取数层：从既有事实表读取观测台需要的原始对象。

只用确认为纯读的路径（``engine.connect()`` 与既有只读 Repository），不触发任何写
事务或行锁。聚合口径保持既有事实原样，格式化交给 ``serializers``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.engine import Engine

from investment_manager.entrypoints.dashboard.pagination import PageCursor, older_than
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.executor import (
    AssessmentExecution,
    AssessmentExecutionStatus,
)
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.tables import (
    assessment_executions,
    codex_account_capacity,
    codex_account_leases,
    codex_runs,
    context_assessments,
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_slot_obligations,
    forecasts,
)
from investment_manager.governance.models import (
    ReleaseManifest,
    validate_manifest_component_versions,
)
from investment_manager.governance.tables import release_manifests
from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.tables import normalized_events
from investment_manager.market.tables import (
    market_quotes,
    market_trades,
    perpetual_market_states,
    perpetual_quotes,
)
from investment_manager.platform.time import database_utc
from investment_manager.scheduling.models import AnalysisTriggerPlan
from investment_manager.scheduling.tables import (
    analysis_trigger_events,
    analysis_trigger_plans,
    trigger_outbox,
)
from investment_manager.settings import AppConfig
from investment_manager.state.decision.packet import DecisionPacket
from investment_manager.state.panel import sanitize_external_text
from investment_manager.state.tables import decision_packets

# 世界事件→AI 输入反向关联的 Packet 扫描上界：linkage 只是尽力而为的标注，
# 加上界避免从一条从未入选的新闻退化为全表扫描。
_EVIDENCE_PACKET_SCAN_LIMIT = 500
_ASSESSMENT_QUALITY_SCAN_LIMIT = 500
_ASSESSMENT_QUALITY_WINDOW_HOURS = 24


def _is_assessment_rejection(reason_code: str) -> bool:
    return reason_code == "CODEX_SCHEMA_INVALID" or reason_code.startswith("ASSESSMENT_")


@dataclass(frozen=True, slots=True)
class AssessmentRecord:
    """One current WorldModel and the packet that produced it."""

    assessment: ContextAssessment
    packet: DecisionPacket | None = None


@dataclass(frozen=True, slots=True)
class AssessmentQualityStatus:
    latest_attempt_at: datetime | None
    latest_attempt_status: str
    latest_attempt_reason: str | None
    latest_valid_at: datetime | None
    rejected_attempt_count_24h: int
    rejection_reason_codes: tuple[str, ...]
    execution_count_24h: int = 0
    final_success_count_24h: int = 0
    first_attempt_success_count_24h: int = 0


@dataclass(frozen=True, slots=True)
class WorldEvent:
    """世界事件时间线一行：一条采集到的新闻或一个触发事件。"""

    event_id: str
    kind: str  # Evidence or typed scheduling event; API preserves the exact kind.
    at: datetime
    source: str
    title: str
    symbols: tuple[str, ...]
    attention_priority: float | None
    injection_suspected: bool
    # 新闻使用发现优先级；系统触发使用原始调度优先级。两者不能互相换算。
    priority: int | None = None
    # 若这条新闻被选入某周期的信息面板（喂给了那次分析），记录该周期
    fed_cycle_id: str | None = None
    fed_cycle_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AccountStatus:
    account_id: str
    enabled: bool
    headroom_percent: float | None
    healthy: bool | None
    observed_at: datetime | None
    leased: bool
    recent_failures: int


@dataclass(frozen=True, slots=True)
class TokenUsageDay:
    date: date
    total_tokens: int


@dataclass(frozen=True, slots=True)
class AccountTokenUsage:
    account_id: str
    total_tokens: int
    daily: tuple[TokenUsageDay, ...]


@dataclass(frozen=True, slots=True)
class TokenUsageWindow:
    window_days: int
    start_date: date
    end_date: date
    total_tokens: int
    daily: tuple[TokenUsageDay, ...]
    accounts: tuple[AccountTokenUsage, ...]


@dataclass(frozen=True, slots=True)
class AnalysisScopeRuntimeStatus:
    symbol: str
    latest_success_at: datetime | None
    heartbeat_seconds: int | None
    trigger_plan_revision: int | None = None
    trigger_plan_origin: str | None = None


@dataclass(frozen=True, slots=True)
class AnalysisRuntimeStatus:
    recent_attempts: int
    recent_successes: int
    pending_outbox_count: int
    oldest_pending_outbox_at: datetime | None
    release_aligned: bool | None
    overdue_forecast_count: int
    oldest_overdue_analysis_at: datetime | None
    scopes: tuple[AnalysisScopeRuntimeStatus, ...]


class DashboardReader:
    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config

    def list_assessments(
        self,
        *,
        cursor: PageCursor | None,
        limit: int,
    ) -> list[AssessmentRecord]:
        query = (
            select(context_assessments.c.payload)
            .order_by(
                context_assessments.c.available_at.desc(),
                context_assessments.c.assessment_id.desc(),
            )
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(
                older_than(
                    context_assessments.c.available_at,
                    context_assessments.c.assessment_id,
                    cursor,
                )
            )
        with self._engine.connect() as connection:
            payloads = connection.execute(query).scalars().all()
        assessments = (ContextAssessment.model_validate(payload) for payload in payloads)
        return [AssessmentRecord(assessment=assessment) for assessment in assessments]

    def get_assessment(self, assessment_id: str) -> AssessmentRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        context_assessments.c.payload.label("assessment_payload"),
                        decision_packets.c.payload.label("packet_payload"),
                    )
                    .join(
                        decision_packets,
                        decision_packets.c.packet_id == context_assessments.c.packet_id,
                    )
                    .where(
                        context_assessments.c.assessment_id == assessment_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
        assessment = ContextAssessment.model_validate(row["assessment_payload"])
        return AssessmentRecord(
            assessment=assessment,
            packet=DecisionPacket.model_validate(row["packet_payload"]),
        )

    def assessment_quality_status(self, *, now: datetime) -> AssessmentQualityStatus:
        """Expose rejected AI outputs without returning their untrusted body to the UI."""

        behavior_hash = configured_assess_behavior_hash(self._config)
        cutoff = now - timedelta(hours=_ASSESSMENT_QUALITY_WINDOW_HOURS)
        with self._engine.connect() as connection:
            assessment_rows = connection.execute(
                select(
                    context_assessments.c.payload,
                    context_assessments.c.available_at,
                    context_assessments.c.analysis_behavior_hash,
                )
                .where(
                    context_assessments.c.available_at >= cutoff,
                    context_assessments.c.available_at <= now,
                )
                .order_by(
                    context_assessments.c.available_at.desc(),
                    context_assessments.c.assessment_id.desc(),
                )
                .limit(_ASSESSMENT_QUALITY_SCAN_LIMIT)
            ).all()
            attempt_rows = connection.execute(
                select(
                    codex_runs.c.status,
                    codex_runs.c.error_class,
                    codex_runs.c.payload,
                    decision_packets.c.as_of,
                )
                .select_from(
                    codex_runs.join(
                        decision_packets,
                        decision_packets.c.packet_id == codex_runs.c.cycle_id,
                    )
                )
                .where(
                    decision_packets.c.as_of >= cutoff,
                    decision_packets.c.as_of <= now,
                    codex_runs.c.payload["analysis_behavior_hash"].as_string() == behavior_hash,
                )
                .order_by(
                    codex_runs.c.payload["completed_at"].as_string().desc(),
                    codex_runs.c.run_id.desc(),
                )
                .limit(_ASSESSMENT_QUALITY_SCAN_LIMIT)
            ).all()
            execution_payloads = tuple(
                connection.execute(
                    select(assessment_executions.c.payload)
                    .where(
                        assessment_executions.c.completed_at >= cutoff,
                        assessment_executions.c.completed_at <= now,
                        assessment_executions.c.analysis_behavior_hash == behavior_hash,
                    )
                    .order_by(
                        assessment_executions.c.completed_at.desc(),
                        assessment_executions.c.execution_id.desc(),
                    )
                    .limit(_ASSESSMENT_QUALITY_SCAN_LIMIT)
                ).scalars()
            )

        latest_valid_at = None
        for payload, available_at, row_behavior_hash in assessment_rows:
            ContextAssessment.model_validate(payload)
            if row_behavior_hash == behavior_hash and latest_valid_at is None:
                latest_valid_at = database_utc(available_at)

        executions = tuple(
            AssessmentExecution.model_validate(payload) for payload in execution_payloads
        )
        measured = tuple(item for item in executions if not item.reused_authoritative)
        rejected_executions = tuple(
            item for item in measured if item.status == AssessmentExecutionStatus.FAILED
        )
        rejected_attempts = [row for row in attempt_rows if row.error_class == "SCHEMA_INVALID"]
        reason_codes = tuple(
            sorted(
                {
                    *(item.reason_code for item in rejected_executions),
                    *("CODEX_SCHEMA_INVALID" for _ in rejected_attempts if not executions),
                }
            )
        )
        if executions:
            latest_execution = executions[0]
            latest_status = latest_execution.status.value
            if latest_status == "FAILED" and _is_assessment_rejection(latest_execution.reason_code):
                latest_status = "REJECTED"
            return AssessmentQualityStatus(
                latest_attempt_at=latest_execution.completed_at,
                latest_attempt_status=latest_status,
                latest_attempt_reason=(
                    None
                    if latest_execution.status == AssessmentExecutionStatus.SUCCEEDED
                    else latest_execution.reason_code
                ),
                latest_valid_at=latest_valid_at,
                rejected_attempt_count_24h=len(rejected_executions),
                rejection_reason_codes=reason_codes,
                execution_count_24h=len(measured),
                final_success_count_24h=sum(
                    item.status == AssessmentExecutionStatus.SUCCEEDED for item in measured
                ),
                first_attempt_success_count_24h=sum(
                    item.status == AssessmentExecutionStatus.SUCCEEDED and item.codex_attempts == 1
                    for item in measured
                ),
            )
        if not attempt_rows:
            return AssessmentQualityStatus(
                latest_attempt_at=None,
                latest_attempt_status="NO_ATTEMPT",
                latest_attempt_reason=None,
                latest_valid_at=latest_valid_at,
                rejected_attempt_count_24h=0,
                rejection_reason_codes=reason_codes,
            )

        latest = attempt_rows[0]
        completed_at = latest.payload.get("completed_at")
        latest_attempt_at = (
            database_utc(datetime.fromisoformat(completed_at))
            if isinstance(completed_at, str)
            else database_utc(latest.as_of)
        )
        latest_status = str(latest.status)
        if latest_status == "FAILED" and latest.error_class == "SCHEMA_INVALID":
            latest_status = "REJECTED"
        return AssessmentQualityStatus(
            latest_attempt_at=latest_attempt_at,
            latest_attempt_status=latest_status,
            latest_attempt_reason=(None if latest.error_class is None else str(latest.error_class)),
            latest_valid_at=latest_valid_at,
            rejected_attempt_count_24h=len(rejected_attempts),
            rejection_reason_codes=reason_codes,
        )

    # --- 世界事件时间线 ---------------------------------------------------
    def list_events(self, *, cursor: PageCursor | None, limit: int) -> list[WorldEvent]:
        news = self._recent_news(cursor=cursor, limit=limit)
        triggers = self._recent_triggers(cursor=cursor, limit=limit)
        merged = sorted(
            news + triggers,
            key=lambda event: (event.at, event.event_id),
            reverse=True,
        )
        return merged[:limit]

    def _recent_news(self, *, cursor: PageCursor | None, limit: int) -> list[WorldEvent]:
        cursor_identity = literal("NEWS:") + normalized_events.c.evidence_id
        query = (
            select(normalized_events.c.payload)
            .order_by(
                normalized_events.c.event_time.desc(),
                normalized_events.c.evidence_id.desc(),
            )
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(older_than(normalized_events.c.event_time, cursor_identity, cursor))
        with self._engine.connect() as connection:
            payloads = connection.execute(query).scalars().all()
        parsed = tuple(IntelligenceEvent.model_validate(payload) for payload in payloads)
        fed = self._evidence_to_cycle(parsed)
        events: list[WorldEvent] = []
        for event in parsed:
            link = fed.get(event.evidence_id)
            _, title_suspicious = sanitize_external_text(event.title, maximum_length=240)
            _, body_suspicious = sanitize_external_text(event.body)
            events.append(
                WorldEvent(
                    event_id=f"NEWS:{event.evidence_id}",
                    kind="NEWS",
                    at=event.event_time,
                    source=event.source,
                    title=event.title,
                    symbols=event.symbols,
                    attention_priority=float(event.attention_priority),
                    injection_suspected=title_suspicious or body_suspicious,
                    fed_cycle_id=link[0] if link else None,
                    fed_cycle_at=link[1] if link else None,
                )
            )
        return events

    def _evidence_to_cycle(
        self, events: tuple[IntelligenceEvent, ...]
    ) -> dict[str, tuple[str, datetime]]:
        """证据 evidence_id → 最早选入它的冻结 AI Packet。

        当前 WorldModel 的唯一真实输入是 ``DecisionPacket``。旧
        ``panel_snapshots`` 属于已经退役的投资链，继续扫描它会把历史面板误写成
        当前 AI 血缘，或漏掉所有现役分析引用。
        """

        if not events:
            return {}
        wanted = {event.evidence_id for event in events}
        first_visible_at = min(event.observed_at for event in events)
        # 一条新闻通常被它出现后的最早 Packet 选中，因此从 first_visible_at 起正序
        # 扫描并在 wanted 集满时停止。多数新闻不会入选，必须保留有界查询。
        query = (
            select(
                decision_packets.c.packet_id,
                decision_packets.c.as_of,
                decision_packets.c.payload,
            )
            .where(decision_packets.c.as_of >= first_visible_at)
            .order_by(decision_packets.c.as_of.asc(), decision_packets.c.packet_id.asc())
            .limit(_EVIDENCE_PACKET_SCAN_LIMIT)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        mapping: dict[str, tuple[str, datetime]] = {}
        for packet_id, as_of, payload in rows:
            if not isinstance(payload, dict):
                continue
            for item in payload.get("intelligence_events", ()):
                evidence_id = item.get("evidence_id") if isinstance(item, dict) else None
                if evidence_id in wanted and evidence_id not in mapping:
                    mapping[evidence_id] = (packet_id, database_utc(as_of))
            if len(mapping) == len(wanted):
                break
        return mapping

    def _recent_triggers(
        self,
        *,
        cursor: PageCursor | None,
        limit: int,
    ) -> list[WorldEvent]:
        cursor_identity = literal("TRIGGER:") + analysis_trigger_events.c.trigger_id
        query = (
            select(
                analysis_trigger_events.c.trigger_id,
                analysis_trigger_events.c.trigger_type,
                analysis_trigger_events.c.symbol,
                analysis_trigger_events.c.occurred_at,
                analysis_trigger_events.c.priority,
                analysis_trigger_events.c.payload,
            )
            .where(
                analysis_trigger_events.c.trigger_type != "INTELLIGENCE_INSERTED",
            )
            .order_by(
                analysis_trigger_events.c.occurred_at.desc(),
                analysis_trigger_events.c.trigger_id.desc(),
            )
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(
                older_than(analysis_trigger_events.c.occurred_at, cursor_identity, cursor)
            )
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        labels = {
            "MARKET_SHOCK": "价格波动触发市场复核",
            "CANONICAL_FACT_REVISED": "关键事实修订触发重新分析",
            "HEARTBEAT": "例行状态检查",
            "FORECAST_SLOT_DUE": "合同预测时点到期",
            "FORECAST_EVENT_DUE": "材料变化触发预测更新",
        }
        sources = {
            "MARKET_SHOCK": "Binance 行情",
            "AGENT_WAKEUP": "主 Agent",
            "CANONICAL_FACT_REVISED": "事实协调器",
            "HEARTBEAT": "系统调度",
            "FORECAST_SLOT_DUE": "预测调度",
            "FORECAST_EVENT_DUE": "世界认知",
        }
        events: list[WorldEvent] = []
        for row in rows:
            trigger_type = row["trigger_type"]
            symbol = row["symbol"]
            payload = row["payload"]
            review_reason = payload.get("review_reason") if isinstance(payload, dict) else None
            if trigger_type == "AGENT_WAKEUP":
                reason = (
                    review_reason.strip()
                    if isinstance(review_reason, str) and review_reason.strip()
                    else "基于最新信息重新评估"
                )
                title = f"{symbol} · 请求原因：{reason}"
            else:
                title = f"{symbol} · {labels.get(trigger_type, '系统事件触发重新分析')}"
            events.append(
                WorldEvent(
                    event_id=f"TRIGGER:{row['trigger_id']}",
                    kind=trigger_type,
                    at=database_utc(row["occurred_at"]),
                    source=sources.get(trigger_type, "系统调度"),
                    title=title,
                    symbols=(symbol,),
                    attention_priority=None,
                    injection_suspected=False,
                    priority=row["priority"],
                )
            )
        return events

    def latest_market_observed_at(self) -> datetime | None:
        """返回所有配置品种报价与成交中最旧的最新观测时间。

        健康度必须读取实时行情事实，不能把最近一次分析周期的冻结指标当作数据流状态。
        品种上限为 20，点查可稳定命中现有索引，成本不随历史表长度线性增长。
        """

        observed: list[datetime] = []
        with self._engine.connect() as connection:
            for symbol in self._config.market_data.symbols:
                quote_at = connection.execute(
                    select(market_quotes.c.observed_at)
                    .where(market_quotes.c.symbol == symbol)
                    .order_by(market_quotes.c.observed_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                trade_at = connection.execute(
                    select(market_trades.c.observed_at)
                    .where(market_trades.c.symbol == symbol)
                    .order_by(market_trades.c.aggregate_trade_id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if quote_at is None or trade_at is None:
                    return None
                observed.extend((database_utc(quote_at), database_utc(trade_at)))
        return min(observed) if observed else None

    def latest_perpetual_observed_at(self) -> datetime | None:
        """Return the oldest latest state/quote across perpetual instruments."""

        instruments = self._config.market_data.perpetual_instruments
        if not instruments:
            return None
        observed: list[datetime] = []
        with self._engine.connect() as connection:
            for instrument in instruments:
                state_at = connection.execute(
                    select(perpetual_market_states.c.observed_at)
                    .where(perpetual_market_states.c.instrument_id == instrument.key)
                    .order_by(perpetual_market_states.c.exchange_time.desc())
                    .limit(1)
                ).scalar_one_or_none()
                quote_at = connection.execute(
                    select(perpetual_quotes.c.observed_at)
                    .where(perpetual_quotes.c.instrument_id == instrument.key)
                    .order_by(perpetual_quotes.c.exchange_time.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if state_at is None or quote_at is None:
                    return None
                observed.extend((database_utc(state_at), database_utc(quote_at)))
        return min(observed)

    # --- AI 账号 ----------------------------------------------------------
    def accounts(self, *, now: datetime) -> list[AccountStatus]:
        capacity = self._latest_capacity()
        leased = self._active_leases(now=now)
        failures = self._recent_failures(now=now)
        statuses: list[AccountStatus] = []
        for account in self._config.codex_accounts.accounts:
            cap = capacity.get(account.account_id)
            observed_at = database_utc(cap[2]) if cap is not None else None
            capacity_fresh = (
                observed_at is not None
                and 0
                <= (now - observed_at).total_seconds()
                <= self._config.codex_runtime.capacity_ttl_seconds
            )
            statuses.append(
                AccountStatus(
                    account_id=account.account_id,
                    enabled=account.enabled,
                    headroom_percent=float(cap[0]) if cap is not None else None,
                    healthy=bool(cap[1]) if cap is not None and capacity_fresh else None,
                    observed_at=observed_at,
                    leased=account.account_id in leased,
                    recent_failures=failures.get(account.account_id, 0),
                )
            )
        return statuses

    def ai_calls_last_hour(self, *, now: datetime) -> int:
        # 账号活动只统计真实 Codex 尝试；调度准入仅用于协调，不能替代运行事实。
        query = (
            select(func.count())
            .select_from(codex_runs)
            .where(
                codex_runs.c.observed_at > now - timedelta(hours=1),
                codex_runs.c.observed_at <= now,
            )
        )
        with self._engine.connect() as connection:
            return int(connection.execute(query).scalar_one())

    def ai_token_usage(self, *, now: datetime, window_days: int = 7) -> TokenUsageWindow:
        """Aggregate reported Codex token usage by UTC day and configured account."""

        if window_days < 1:
            raise ValueError("AI token 用量窗口必须至少包含一天")
        now = database_utc(now)
        end_date = now.date()
        start_date = end_date - timedelta(days=window_days - 1)
        start_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=window_days - 1
        )
        dates = tuple(start_date + timedelta(days=offset) for offset in range(window_days))
        account_ids = tuple(account.account_id for account in self._config.codex_accounts.accounts)
        by_account = {account_id: {day: 0 for day in dates} for account_id in account_ids}

        if account_ids:
            query = select(
                codex_runs.c.account_id,
                codex_runs.c.observed_at,
                codex_runs.c.payload,
            ).where(
                codex_runs.c.account_id.in_(account_ids),
                codex_runs.c.observed_at >= start_at,
                codex_runs.c.observed_at <= now,
            )
            with self._engine.connect() as connection:
                rows = connection.execute(query).all()
            for account_id, observed_at, payload in rows:
                if account_id is None:
                    continue
                day = database_utc(observed_at).date()
                if day in by_account[account_id]:
                    by_account[account_id][day] += self._reported_total_tokens(payload)

        account_usage = tuple(
            AccountTokenUsage(
                account_id=account_id,
                total_tokens=sum(by_account[account_id].values()),
                daily=tuple(
                    TokenUsageDay(date=day, total_tokens=by_account[account_id][day])
                    for day in dates
                ),
            )
            for account_id in account_ids
        )
        daily = tuple(
            TokenUsageDay(
                date=day,
                total_tokens=sum(by_account[account_id][day] for account_id in account_ids),
            )
            for day in dates
        )
        return TokenUsageWindow(
            window_days=window_days,
            start_date=start_date,
            end_date=end_date,
            total_tokens=sum(item.total_tokens for item in daily),
            daily=daily,
            accounts=account_usage,
        )

    @staticmethod
    def _reported_total_tokens(payload: object) -> int:
        if not isinstance(payload, Mapping):
            return 0
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return 0
        value = usage.get("total_tokens")
        if isinstance(value, bool):
            return 0
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, tokens)

    def analysis_runtime_status(self, *, now: datetime) -> AnalysisRuntimeStatus:
        """读取当前 Pipeline 的最小控制面健康事实，不扫描历史正文。"""

        pipeline = self._config.pipeline.version
        recent_start = now - timedelta(hours=1)
        scopes = tuple(f"{symbol}:{pipeline}" for symbol in self._config.analysis_symbols)
        behavior_hash = configured_assess_behavior_hash(self._config)
        with self._engine.connect() as connection:
            recent_rows = connection.execute(
                select(codex_runs.c.status, codex_runs.c.payload)
                .join(
                    decision_packets,
                    decision_packets.c.packet_id == codex_runs.c.cycle_id,
                )
                .where(
                    decision_packets.c.as_of >= recent_start,
                    decision_packets.c.as_of <= now,
                    codex_runs.c.payload["analysis_behavior_hash"].as_string()
                    == behavior_hash,
                )
            ).all()
            pending_count, oldest_pending = connection.execute(
                select(func.count(), func.min(trigger_outbox.c.available_at)).where(
                    trigger_outbox.c.status == "PENDING",
                    trigger_outbox.c.aggregate_key.in_(scopes),
                    trigger_outbox.c.available_at <= now,
                )
            ).one()
            plan_rows = connection.execute(
                select(
                    analysis_trigger_plans.c.symbol,
                    analysis_trigger_plans.c.manifest_id,
                    analysis_trigger_plans.c.payload,
                ).where(
                    analysis_trigger_plans.c.pipeline_id == pipeline,
                    analysis_trigger_plans.c.is_current.is_(True),
                )
            ).all()
            manifest_ids = {manifest_id for _, manifest_id, _ in plan_rows}
            manifest_payload = (
                connection.execute(
                    select(release_manifests.c.payload).where(
                        release_manifests.c.manifest_id == next(iter(manifest_ids))
                    )
                ).scalar_one_or_none()
                if len(manifest_ids) == 1
                else None
            )
            latest_assessment_completed = connection.execute(
                select(func.max(context_assessments.c.available_at)).where(
                    context_assessments.c.analysis_behavior_hash == behavior_hash,
                    context_assessments.c.available_at <= now,
                )
            ).scalar_one_or_none()
            if latest_assessment_completed is not None:
                latest_assessment_completed = database_utc(latest_assessment_completed)
            context = self._config.capital.context_forecast
            forecast_behavior = (
                context.producer_behavior_id if context is not None else "DISABLED"
            )
            overdue_analyses = (
                select(
                    forecast_slot_obligations.c.obligation_id.label("proposal_id"),
                    forecast_decision_slots.c.completion_deadline_at.label("analysis_at"),
                )
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id
                    == forecast_slot_obligations.c.slot_id,
                )
                .outerjoin(
                    forecasts,
                    (forecasts.c.decision_slot_id == forecast_slot_obligations.c.slot_id)
                    & (
                        forecasts.c.producer_behavior_id
                        == forecast_slot_obligations.c.producer_behavior_id
                    ),
                )
                .outerjoin(
                    forecast_no_estimates,
                    (forecast_no_estimates.c.slot_id == forecast_slot_obligations.c.slot_id)
                    & (
                        forecast_no_estimates.c.producer_behavior_id
                        == forecast_slot_obligations.c.producer_behavior_id
                    ),
                )
                .where(
                    forecast_slot_obligations.c.producer_behavior_id
                    == forecast_behavior,
                    forecast_decision_slots.c.completion_deadline_at <= now,
                    forecasts.c.forecast_id.is_(None),
                    forecast_no_estimates.c.result_id.is_(None),
                )
                .subquery()
            )
            overdue_count, oldest_overdue = connection.execute(
                select(
                    func.count(),
                    func.min(overdue_analyses.c.analysis_at),
                ).select_from(overdue_analyses)
            ).one()

            plan_by_symbol = {symbol: payload for symbol, _, payload in plan_rows}
            scope_statuses: list[AnalysisScopeRuntimeStatus] = []
            for symbol in self._config.analysis_symbols:
                completed = latest_assessment_completed
                plan_payload = plan_by_symbol.get(symbol)
                plan = (
                    AnalysisTriggerPlan.model_validate(plan_payload)
                    if isinstance(plan_payload, dict)
                    else None
                )
                heartbeat = plan.heartbeat_seconds if plan is not None else None
                scope_statuses.append(
                    AnalysisScopeRuntimeStatus(
                        symbol=symbol,
                        latest_success_at=completed,
                        heartbeat_seconds=(
                            heartbeat if isinstance(heartbeat, int) and heartbeat > 0 else None
                        ),
                        trigger_plan_revision=plan.revision if plan is not None else None,
                        trigger_plan_origin=(
                            plan.origin.value if plan is not None else None
                        ),
                    )
                )

        expected_symbols = set(self._config.analysis_symbols)
        actual_symbols = {symbol for symbol, _, _ in plan_rows}
        if not plan_rows or manifest_payload is None:
            release_aligned = None
        else:
            release_aligned = actual_symbols == expected_symbols and len(plan_rows) == len(
                expected_symbols
            )
            if release_aligned:
                try:
                    validate_manifest_component_versions(
                        ReleaseManifest.model_validate(manifest_payload),
                        self._config,
                    )
                except (TypeError, ValueError):
                    release_aligned = False
        return AnalysisRuntimeStatus(
            recent_attempts=len(recent_rows),
            recent_successes=sum(status == "SUCCEEDED" for status, _ in recent_rows),
            pending_outbox_count=int(pending_count),
            oldest_pending_outbox_at=(
                database_utc(oldest_pending) if oldest_pending is not None else None
            ),
            release_aligned=release_aligned,
            overdue_forecast_count=int(overdue_count),
            oldest_overdue_analysis_at=(
                database_utc(oldest_overdue) if oldest_overdue is not None else None
            ),
            scopes=tuple(scope_statuses),
        )

    def _latest_capacity(self) -> dict[str, tuple]:
        # 白名单账号数量很小且有 (account_id, observed_at) 主键；点查避免每次刷新
        # 对全部容量历史做窗口排序。
        latest: dict[str, tuple] = {}
        with self._engine.connect() as connection:
            for account in self._config.codex_accounts.accounts:
                row = connection.execute(
                    select(
                        codex_account_capacity.c.effective_headroom,
                        codex_account_capacity.c.healthy,
                        codex_account_capacity.c.observed_at,
                    )
                    .where(codex_account_capacity.c.account_id == account.account_id)
                    .order_by(codex_account_capacity.c.observed_at.desc())
                    .limit(1)
                ).first()
                if row is not None:
                    latest[account.account_id] = tuple(row)
        return latest

    def _active_leases(self, *, now: datetime) -> set[str]:
        with self._engine.connect() as connection:
            return set(
                connection.execute(
                    select(codex_account_leases.c.account_id).where(
                        codex_account_leases.c.status == "ACTIVE",
                        codex_account_leases.c.expires_at > now,
                    )
                ).scalars()
            )

    def _recent_failures(self, *, now: datetime) -> dict[str, int]:
        if not self._config.assessment.enabled:
            return {}
        behavior_hash = configured_assess_behavior_hash(self._config)
        query = (
            select(codex_runs.c.account_id, func.count())
            .select_from(
                codex_runs.join(
                    decision_packets,
                    decision_packets.c.packet_id == codex_runs.c.cycle_id,
                )
            )
            .where(
                decision_packets.c.as_of >= now - timedelta(hours=1),
                decision_packets.c.as_of <= now,
                codex_runs.c.payload["analysis_behavior_hash"].as_string() == behavior_hash,
                codex_runs.c.status != "SUCCEEDED",
                codex_runs.c.account_id.is_not(None),
            )
            .group_by(codex_runs.c.account_id)
        )
        with self._engine.connect() as connection:
            return {account_id: count for account_id, count in connection.execute(query).all()}
