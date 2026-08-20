from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.tables import normalized_events
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    build_trigger_event,
)
from investment_manager.scheduling.repository import insert_trigger_with_outbox
from investment_manager.scheduling.tables import analysis_trigger_events


class SqlEventStore:
    """标准事件事实；新增事件与每品种 Trigger/Outbox 在同一事务提交。"""

    def __init__(
        self,
        engine: Engine,
        *,
        pipeline_id: str = "default",
        trigger_expiry_seconds: int = 900,
        max_visible_events: int = 100,
    ) -> None:
        if trigger_expiry_seconds < 1:
            raise ValueError("事件触发有效期必须为正数")
        if not 1 <= max_visible_events <= 1000:
            raise ValueError("可见事件读取上限必须在 1..1000")
        self._engine = engine
        self._pipeline_id = pipeline_id
        self._trigger_expiry_seconds = trigger_expiry_seconds
        self._max_visible_events = max_visible_events

    def put(self, event: IntelligenceEvent) -> bool:
        payload = event.model_dump(mode="json")
        digest = content_hash({"title": event.title, "body": event.body})
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(normalized_events).values(
                        evidence_id=event.evidence_id,
                        event_time=event.event_time,
                        observed_at=event.observed_at,
                        source=event.source,
                        content_hash=digest,
                        payload=payload,
                    )
                )
                for symbol in event.symbols:
                    trigger = build_trigger_event(
                        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                        symbol=symbol,
                        pipeline_id=self._pipeline_id,
                        occurred_at=event.event_time,
                        observed_at=event.observed_at,
                        priority=int(event.impact * 100),
                        dedup_key=event.evidence_id,
                        evidence_ids=(event.evidence_id,),
                        expires_at=event.observed_at
                        + timedelta(seconds=self._trigger_expiry_seconds),
                    )
                    insert_trigger_with_outbox(connection, trigger)
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(normalized_events.c.payload).where(
                        (normalized_events.c.evidence_id == event.evidence_id)
                        | (
                            (normalized_events.c.source == event.source)
                            & (normalized_events.c.content_hash == digest)
                        )
                    )
                ).scalar_one_or_none()
            if existing is not None and existing != payload:
                same_content = (
                    existing.get("source") == payload["source"]
                    and existing.get("title") == payload["title"]
                    and existing.get("body") == payload["body"]
                )
                if not same_content:
                    raise ValueError("事件唯一键冲突且事实不一致") from None
            return False
        return True

    def visible(self, *, symbol: str, as_of: datetime) -> tuple[IntelligenceEvent, ...]:
        as_of = require_utc(as_of)
        # Trigger 激活必须按 pipeline 隔离，但已观测到的世界事实不应在每次
        # 发布时清空。这里只复用历史 Trigger 中的品种路由事实，不会让旧
        # pipeline 的触发器、待处理批次或调用准入进入新 pipeline。
        routed_evidence = (
            select(analysis_trigger_events.c.dedup_key.label("evidence_id"))
            .where(
                analysis_trigger_events.c.trigger_type == "INTELLIGENCE_INSERTED",
                analysis_trigger_events.c.symbol == symbol,
                analysis_trigger_events.c.observed_at <= as_of,
            )
            .distinct()
            .subquery()
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(normalized_events.c.payload)
                .select_from(
                    normalized_events.join(
                        routed_evidence,
                        routed_evidence.c.evidence_id == normalized_events.c.evidence_id,
                    )
                )
                .where(
                    normalized_events.c.observed_at <= as_of,
                )
                .order_by(
                    normalized_events.c.event_time.desc(),
                    normalized_events.c.evidence_id.desc(),
                )
                .limit(self._max_visible_events)
            ).scalars()
            events = tuple(IntelligenceEvent.model_validate(item) for item in rows)
        return tuple(sorted(events, key=lambda item: (item.event_time, item.evidence_id)))

    def exact(
        self,
        *,
        evidence_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[IntelligenceEvent, ...]:
        as_of = require_utc(as_of)
        if tuple(sorted(set(evidence_ids))) != evidence_ids:
            raise ValueError("evidence_ids 必须唯一且排序")
        if not evidence_ids:
            return ()
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(
                    normalized_events.c.evidence_id,
                    normalized_events.c.payload,
                ).where(
                    normalized_events.c.evidence_id.in_(evidence_ids),
                    normalized_events.c.observed_at <= as_of,
                )
            ).all()
        by_id = {
            row.evidence_id: IntelligenceEvent.model_validate(row.payload)
            for row in payloads
        }
        missing = tuple(item for item in evidence_ids if item not in by_id)
        if missing:
            raise ValueError("缺少截至 as_of 可见的事件: " + ", ".join(missing))
        return tuple(by_id[item] for item in evidence_ids)

