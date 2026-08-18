"""只读取数层：从既有事实表读取观测台需要的原始对象。

只用确认为纯读的路径（``engine.connect()`` 与既有只读 Repository），不触发任何写
事务或行锁。聚合口径保持既有事实原样，格式化交给 ``serializers``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from quant_core.config import AppConfig
from quant_core.domain import (
    AnalysisProposal,
    IntelligenceEvent,
    MetricObservation,
    TradeIntent,
)
from quant_core.evaluation import OutcomeWindowEvaluator, OutcomeWindowReport
from quant_core.ledger import CycleFacts
from quant_core.lifecycle import OpenLifecycleRecord
from quant_core.outcome_evaluation_sql import OutcomeWindowFacts, SqlOutcomeWindowRepository
from quant_core.persistence import (
    SqlFactLedger,
    SqlOpenLifecycleRepository,
    analysis_cycles,
    analysis_proposals,
    analysis_trigger_events,
    codex_account_capacity,
    codex_account_leases,
    codex_runs,
    market_snapshots,
    metric_observations,
    normalized_events,
    orders,
    panel_snapshots,
    trade_intents,
)
from quant_core.reconciliation import ReconciliationReport
from quant_core.reconciliation_sql import SqlReconciliationReportStore


@dataclass(frozen=True, slots=True)
class CycleRow:
    """决策时间线一行所需的最小事实（不含整张周期图）。"""

    cycle_id: str
    as_of: datetime
    symbol: str
    outcome: str
    reason_code: str
    proposal: AnalysisProposal | None
    intent: TradeIntent | None


@dataclass(frozen=True, slots=True)
class WorldEvent:
    """世界事件时间线一行：一条采集到的新闻或一个触发事件。"""

    kind: str  # "NEWS" | "MARKET_SHOCK" | "POSITION_RECHECK" | "INTELLIGENCE_INSERTED"
    at: datetime
    source: str
    title: str
    symbols: tuple[str, ...]
    impact: float | None
    injection_suspected: bool
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
class EquityWindow:
    facts: OutcomeWindowFacts
    # 用生产同一 evaluator 就所选窗口算出的报告：曲线与指标口径一致，公式不重复。
    report: OutcomeWindowReport
    lookback_start: datetime
    lookback_end: datetime


class DashboardReader:
    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._ledger = SqlFactLedger(engine)
        self._lifecycles = SqlOpenLifecycleRepository(engine)
        self._reconciliation = SqlReconciliationReportStore(engine)
        self._outcomes = SqlOutcomeWindowRepository(engine)
        self._evaluator = OutcomeWindowEvaluator(version=config.outcome_evaluation.version)

    # --- 决策时间线 -------------------------------------------------------
    def list_cycles(self, *, before: datetime | None, limit: int) -> list[CycleRow]:
        query = (
            select(
                analysis_cycles.c.cycle_id,
                analysis_cycles.c.as_of,
                analysis_cycles.c.outcome,
                analysis_cycles.c.reason_code,
                market_snapshots.c.symbol,
                analysis_proposals.c.payload.label("proposal_payload"),
                trade_intents.c.payload.label("intent_payload"),
            )
            .select_from(analysis_cycles)
            .join(market_snapshots, market_snapshots.c.cycle_id == analysis_cycles.c.cycle_id)
            .join(
                analysis_proposals,
                analysis_proposals.c.cycle_id == analysis_cycles.c.cycle_id,
                isouter=True,
            )
            .join(
                trade_intents,
                trade_intents.c.cycle_id == analysis_cycles.c.cycle_id,
                isouter=True,
            )
            .order_by(analysis_cycles.c.as_of.desc(), analysis_cycles.c.cycle_id.desc())
            .limit(limit)
        )
        if before is not None:
            query = query.where(analysis_cycles.c.as_of < before)
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return [
            CycleRow(
                cycle_id=row["cycle_id"],
                as_of=row["as_of"],
                symbol=row["symbol"],
                outcome=row["outcome"],
                reason_code=row["reason_code"],
                proposal=(
                    AnalysisProposal.model_validate(row["proposal_payload"])
                    if row["proposal_payload"] is not None
                    else None
                ),
                intent=(
                    TradeIntent.model_validate(row["intent_payload"])
                    if row["intent_payload"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def get_cycle(self, cycle_id: str) -> CycleFacts | None:
        return self._ledger.get(cycle_id)

    # --- 世界事件时间线 ---------------------------------------------------
    def list_events(self, *, before: datetime | None, limit: int) -> list[WorldEvent]:
        news = self._recent_news(before=before, limit=limit)
        triggers = self._recent_triggers(before=before, limit=limit)
        merged = sorted(news + triggers, key=lambda event: event.at, reverse=True)
        return merged[:limit]

    def _recent_news(self, *, before: datetime | None, limit: int) -> list[WorldEvent]:
        query = (
            select(normalized_events.c.payload)
            .order_by(normalized_events.c.event_time.desc())
            .limit(limit)
        )
        if before is not None:
            query = query.where(normalized_events.c.event_time < before)
        with self._engine.connect() as connection:
            payloads = connection.execute(query).scalars().all()
        fed = self._evidence_to_cycle(limit=limit)
        events: list[WorldEvent] = []
        for payload in payloads:
            event = IntelligenceEvent.model_validate(payload)
            link = fed.get(event.evidence_id)
            events.append(
                WorldEvent(
                    kind="NEWS",
                    at=event.event_time,
                    source=event.source,
                    title=event.title,
                    symbols=event.symbols,
                    impact=float(event.impact),
                    injection_suspected=False,
                    fed_cycle_id=link[0] if link else None,
                    fed_cycle_at=link[1] if link else None,
                )
            )
        return events

    def _evidence_to_cycle(self, *, limit: int) -> dict[str, tuple[str, datetime]]:
        """证据 evidence_id → 选入它的周期。一条新闻若进入某周期面板，即喂给了那次分析。"""

        query = (
            select(panel_snapshots.c.cycle_id, panel_snapshots.c.as_of, panel_snapshots.c.payload)
            .order_by(panel_snapshots.c.as_of.desc())
            .limit(limit * 2)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        mapping: dict[str, tuple[str, datetime]] = {}
        for cycle_id, as_of, payload in rows:
            if not isinstance(payload, dict):
                continue
            for item in payload.get("evidence", ()):
                evidence_id = item.get("evidence_id") if isinstance(item, dict) else None
                if evidence_id and evidence_id not in mapping:
                    mapping[evidence_id] = (cycle_id, as_of)
        return mapping

    def _recent_triggers(self, *, before: datetime | None, limit: int) -> list[WorldEvent]:
        query = (
            select(
                analysis_trigger_events.c.trigger_type,
                analysis_trigger_events.c.symbol,
                analysis_trigger_events.c.occurred_at,
                analysis_trigger_events.c.priority,
            )
            .where(analysis_trigger_events.c.trigger_type != "INTELLIGENCE_INSERTED")
            .order_by(analysis_trigger_events.c.occurred_at.desc())
            .limit(limit)
        )
        if before is not None:
            query = query.where(analysis_trigger_events.c.occurred_at < before)
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        labels = {"MARKET_SHOCK": "市场冲击", "POSITION_RECHECK": "持仓复检"}
        return [
            WorldEvent(
                kind=row["trigger_type"],
                at=row["occurred_at"],
                source="Binance 行情" if row["trigger_type"] == "MARKET_SHOCK" else "生命周期",
                title=f"{labels.get(row['trigger_type'], row['trigger_type'])}（{row['symbol']}）",
                symbols=(row["symbol"],),
                impact=min(1.0, row["priority"] / 100.0),
                injection_suspected=False,
            )
            for row in rows
        ]

    # --- 持仓 / 对账 ------------------------------------------------------
    def open_positions(self) -> tuple[OpenLifecycleRecord, ...]:
        return self._lifecycles.list_open()

    def entry_sides(self, cycle_ids: list[str]) -> dict[str, str]:
        """持仓方向取自各自周期的建仓订单（PositionLifecycle 本身不存 side）。"""

        if not cycle_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(orders.c.cycle_id, orders.c.payload).where(
                    orders.c.role == "ENTRY", orders.c.cycle_id.in_(cycle_ids)
                )
            ).all()
        return {
            cycle_id: payload["side"]
            for cycle_id, payload in rows
            if isinstance(payload, dict) and payload.get("side")
        }

    def latest_prices(self) -> dict[str, Decimal]:
        """每个品种最近一次记录的最新价，用于持仓浮盈的盯市估算（非结算口径）。

        按配置品种逐个取最新一行（有索引、行数有界），不整表扫描。
        """

        prices: dict[str, Decimal] = {}
        with self._engine.connect() as connection:
            for symbol in self._config.market_data.symbols:
                payload = connection.execute(
                    select(market_snapshots.c.payload)
                    .where(market_snapshots.c.symbol == symbol)
                    .order_by(market_snapshots.c.as_of.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if isinstance(payload, dict) and payload.get("last"):
                    prices[symbol] = Decimal(str(payload["last"]))
        return prices

    def latest_reconciliation(self, *, now: datetime) -> ReconciliationReport | None:
        return self._reconciliation.latest(as_of=now)

    # --- 权益 / 结果窗口 --------------------------------------------------
    def equity_window(self, *, now: datetime, hours: int) -> EquityWindow:
        start = now - timedelta(hours=hours)
        pipeline_version = self._config.pipeline.version
        facts = self._outcomes.load(
            pipeline_version=pipeline_version,
            window_start=start,
            window_end=now,
        )
        report = self._evaluator.evaluate(
            pipeline_version=pipeline_version,
            window_start=start,
            window_end=now,
            cycles=facts.cycles,
            outcomes=facts.outcomes,
            unresolved_cycle_ids=facts.unresolved_cycle_ids,
        )
        return EquityWindow(facts=facts, report=report, lookback_start=start, lookback_end=now)

    def latest_cycle_metrics(self) -> tuple[MetricObservation, ...] | None:
        """最近一个周期的指标观测（供健康新鲜度判断，避免装配整张周期图）。"""

        with self._engine.connect() as connection:
            cycle_id = connection.execute(
                select(analysis_cycles.c.cycle_id).order_by(analysis_cycles.c.as_of.desc()).limit(1)
            ).scalar_one_or_none()
            if cycle_id is None:
                return None
            payloads = connection.execute(
                select(metric_observations.c.payload).where(
                    metric_observations.c.cycle_id == cycle_id
                )
            ).scalars()
            return tuple(MetricObservation.model_validate(payload) for payload in payloads)

    # --- AI 账号 ----------------------------------------------------------
    def accounts(self, *, now: datetime) -> list[AccountStatus]:
        capacity = self._latest_capacity()
        leased = self._active_leases()
        failures = self._recent_failures(now=now)
        statuses: list[AccountStatus] = []
        for account in self._config.codex_accounts.accounts:
            cap = capacity.get(account.account_id)
            statuses.append(
                AccountStatus(
                    account_id=account.account_id,
                    enabled=account.enabled,
                    headroom_percent=float(cap[0]) if cap is not None else None,
                    healthy=bool(cap[1]) if cap is not None else None,
                    observed_at=cap[2] if cap is not None else None,
                    leased=account.account_id in leased,
                    recent_failures=failures.get(account.account_id, 0),
                )
            )
        return statuses

    def ai_calls_last_hour(self, *, now: datetime) -> int:
        # codex_runs 无独立时间列，运行时间取 cycle.as_of；在 SQL 侧按窗口联接计数。
        query = (
            select(func.count())
            .select_from(
                codex_runs.join(
                    analysis_cycles, analysis_cycles.c.cycle_id == codex_runs.c.cycle_id
                )
            )
            .where(
                codex_runs.c.attempt == 1,
                analysis_cycles.c.as_of >= now - timedelta(hours=1),
                analysis_cycles.c.as_of <= now,
            )
        )
        with self._engine.connect() as connection:
            return int(connection.execute(query).scalar_one())

    def _latest_capacity(self) -> dict[str, tuple]:
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

    def _active_leases(self) -> set[str]:
        with self._engine.connect() as connection:
            return set(
                connection.execute(
                    select(codex_account_leases.c.account_id).where(
                        codex_account_leases.c.status == "ACTIVE"
                    )
                ).scalars()
            )

    def _recent_failures(self, *, now: datetime) -> dict[str, int]:
        query = (
            select(codex_runs.c.account_id, func.count())
            .select_from(
                codex_runs.join(
                    analysis_cycles, analysis_cycles.c.cycle_id == codex_runs.c.cycle_id
                )
            )
            .where(
                analysis_cycles.c.as_of >= now - timedelta(hours=1),
                analysis_cycles.c.as_of <= now,
                codex_runs.c.status != "SUCCEEDED",
                codex_runs.c.account_id.is_not(None),
            )
            .group_by(codex_runs.c.account_id)
        )
        with self._engine.connect() as connection:
            return {account_id: count for account_id, count in connection.execute(query).all()}
