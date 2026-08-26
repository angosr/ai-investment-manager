"""Offline replay-evaluation persistence, outside the production repository graph."""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.governance.evaluation.performance import ReplayEvaluationReport
from investment_manager.governance.tables import replay_evaluation_reports


class SqlEvaluationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, report: ReplayEvaluationReport) -> None:
        payload = report.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(replay_evaluation_reports).values(
                        report_id=report.report_id,
                        evaluation_version=report.evaluation_version,
                        dataset_hash=report.dataset_hash,
                        statistically_conclusive=report.statistically_conclusive,
                        payload=payload,
                    )
                )
        except IntegrityError:
            existing = self.get(report.report_id)
            if existing != report:
                raise ValueError("相同 report_id 的评估事实不一致") from None

    def get(self, report_id: str) -> ReplayEvaluationReport | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(replay_evaluation_reports.c.payload).where(
                    replay_evaluation_reports.c.report_id == report_id
                )
            ).scalar_one_or_none()
        return ReplayEvaluationReport.model_validate(payload) if payload else None
