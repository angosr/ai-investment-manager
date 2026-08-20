from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.domain import (
    CycleOutcome,
    DecisionOutcome,
)
from investment_manager.evaluation import (
    CycleDecisionFact,
    OutcomeWindowReport,
)
from investment_manager.kernel.time import require_utc
from investment_manager.persistence import (
    analysis_cycles,
    decision_outcomes,
    fills,
    orders,
    outcome_window_reports,
)


@dataclass(frozen=True, slots=True)
class OutcomeWindowFacts:
    cycles: tuple[CycleDecisionFact, ...]
    outcomes: tuple[DecisionOutcome, ...]
    unresolved_cycle_ids: tuple[str, ...]


class SqlOutcomeWindowRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(
        self,
        *,
        pipeline_version: str,
        window_start: datetime,
        window_end: datetime,
    ) -> OutcomeWindowFacts:
        window_start = require_utc(window_start)
        window_end = require_utc(window_end)
        conditions = (
            analysis_cycles.c.pipeline_version == pipeline_version,
            analysis_cycles.c.as_of >= window_start,
            analysis_cycles.c.as_of < window_end,
        )
        with self._engine.connect() as connection:
            cycle_rows = connection.execute(
                select(
                    analysis_cycles.c.cycle_id,
                    analysis_cycles.c.pipeline_version,
                    analysis_cycles.c.outcome,
                    analysis_cycles.c.reason_code,
                )
                .where(*conditions)
                .order_by(analysis_cycles.c.as_of, analysis_cycles.c.cycle_id)
            ).all()
            outcome_payloads = tuple(
                connection.execute(
                    select(decision_outcomes.c.payload)
                    .join(
                        analysis_cycles,
                        analysis_cycles.c.cycle_id == decision_outcomes.c.cycle_id,
                    )
                    .where(*conditions)
                    .order_by(decision_outcomes.c.outcome_id)
                ).scalars()
            )
            filled_cycle_ids = set(
                connection.execute(
                    select(orders.c.cycle_id)
                    .distinct()
                    .join(fills, fills.c.order_id == orders.c.order_id)
                    .join(
                        analysis_cycles,
                        analysis_cycles.c.cycle_id == orders.c.cycle_id,
                    )
                    .where(orders.c.role == "ENTRY", *conditions)
                ).scalars()
            )
        cycles = tuple(
            CycleDecisionFact(
                cycle_id=cycle_id,
                pipeline_version=version,
                outcome=CycleOutcome(outcome),
                reason_code=reason_code,
            )
            for cycle_id, version, outcome, reason_code in cycle_rows
        )
        outcomes = tuple(DecisionOutcome.model_validate(item) for item in outcome_payloads)
        completed_cycle_ids = {item.cycle_id for item in outcomes}
        pending_cycle_ids = {
            item.cycle_id for item in cycles if item.outcome == CycleOutcome.EXECUTION_PENDING
        }
        unresolved = tuple(sorted(pending_cycle_ids | (filled_cycle_ids - completed_cycle_ids)))
        return OutcomeWindowFacts(
            cycles=cycles,
            outcomes=outcomes,
            unresolved_cycle_ids=unresolved,
        )

    def record(self, report: OutcomeWindowReport) -> OutcomeWindowReport:
        payload = report.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(outcome_window_reports).values(
                        report_id=report.report_id,
                        evaluation_version=report.evaluation_version,
                        pipeline_version=report.pipeline_version,
                        window_start=report.window_start,
                        window_end=report.window_end,
                        status=report.status.value,
                        source_hash=report.source_hash,
                        payload=payload,
                    )
                )
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(outcome_window_reports.c.payload).where(
                        outcome_window_reports.c.report_id == report.report_id
                    )
                ).scalar_one()
            if OutcomeWindowReport.model_validate(existing) != report:
                raise ValueError("相同 report_id 的窗口评估事实不一致") from None
        return report

    def latest(
        self,
        *,
        pipeline_version: str,
        window_start: datetime,
        window_end: datetime,
    ) -> OutcomeWindowReport | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(outcome_window_reports.c.payload)
                .where(
                    outcome_window_reports.c.pipeline_version == pipeline_version,
                    outcome_window_reports.c.window_start == require_utc(window_start),
                    outcome_window_reports.c.window_end == require_utc(window_end),
                )
                .order_by(outcome_window_reports.c.report_id.desc())
                .limit(1)
            ).scalar_one_or_none()
        return OutcomeWindowReport.model_validate(payload) if payload is not None else None
