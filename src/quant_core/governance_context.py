from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from quant_core.config import AppConfig
from quant_core.domain import _require_utc
from quant_core.evaluation import OutcomeWindowReport
from quant_core.governance import (
    EvaluationPlan,
    FailedExperiment,
    GovernanceSnapshot,
    ReleaseManifest,
    build_governance_snapshot,
    load_constitution,
    load_release_manifest,
    validate_manifest_against_config,
)
from quant_core.persistence import (
    SqlGovernanceRepository,
    analysis_cycles,
    analysis_trigger_plans,
    architecture_decisions,
    change_proposals,
    codex_runs,
    evaluation_plans,
    failed_experiment_records,
    outcome_window_reports,
    reconciliation_reports,
    release_manifests,
    trigger_outbox,
)
from quant_core.trigger import AnalysisTriggerPlan


class GovernanceSnapshotAssembler:
    """从受限结构化事实构建治理面板，不读取聊天历史或原始全量日志。"""

    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        *,
        project_root: Path,
    ) -> None:
        self._engine = engine
        self._config = config
        self._root = project_root
        self._repository = SqlGovernanceRepository(engine)

    def build(self, *, as_of) -> GovernanceSnapshot:
        as_of = _require_utc(as_of)
        constitution = load_constitution(self._root / "config" / "system-constitution.yaml")
        bootstrap = load_release_manifest(self._root / "config" / "release-manifest.yaml")
        validate_manifest_against_config(bootstrap, self._config)
        if bootstrap.constitution_version != constitution.version:
            raise ValueError("ReleaseManifest 与系统宪法版本不一致")
        self._repository.record_constitution(constitution)
        self._repository.record_release(bootstrap)

        cutoff = as_of - timedelta(days=self._config.governance.snapshot_lookback_days)
        with self._engine.connect() as connection:
            champion_payloads = tuple(
                connection.execute(
                    select(release_manifests.c.payload).where(
                        release_manifests.c.status == "CHAMPION"
                    )
                ).scalars()
            )
            if len(champion_payloads) != 1:
                raise ValueError("治理周期要求数据库中恰好一个 CHAMPION")
            champion = ReleaseManifest.model_validate(champion_payloads[0])
            previous_stable = tuple(
                connection.execute(
                    select(release_manifests.c.manifest_id)
                    .where(release_manifests.c.status == "PREVIOUS_STABLE")
                    .order_by(release_manifests.c.manifest_id)
                ).scalars()
            )
            failed = tuple(
                FailedExperiment.model_validate(payload)
                for payload in connection.execute(
                    select(failed_experiment_records.c.payload)
                    .where(failed_experiment_records.c.rejected_at <= as_of)
                    .order_by(failed_experiment_records.c.rejected_at.desc())
                    .limit(self._config.governance.maximum_failed_experiments)
                ).scalars()
            )
            open_proposals = tuple(
                connection.execute(
                    select(change_proposals.c.proposal_id)
                    .where(change_proposals.c.status == "PROPOSED")
                    .order_by(change_proposals.c.proposal_id)
                    .limit(self._config.governance.maximum_open_proposals)
                ).scalars()
            )
            plans = tuple(
                EvaluationPlan.model_validate(payload)
                for payload in connection.execute(
                    select(evaluation_plans.c.payload)
                    .where(
                        evaluation_plans.c.base_manifest_id == champion.manifest_id,
                        evaluation_plans.c.registered_at <= as_of,
                    )
                    .order_by(evaluation_plans.c.registered_at.desc())
                    .limit(self._config.governance.maximum_evaluation_plans)
                ).scalars()
            )
            architecture_ids = tuple(
                connection.execute(
                    select(architecture_decisions.c.decision_id)
                    .where(architecture_decisions.c.status == "ACCEPTED")
                    .order_by(architecture_decisions.c.decision_id)
                ).scalars()
            )
            window_payloads = tuple(
                connection.execute(
                    select(outcome_window_reports.c.payload)
                    .where(
                        outcome_window_reports.c.window_end >= cutoff,
                        outcome_window_reports.c.window_end <= as_of,
                        outcome_window_reports.c.status == "COMPLETE",
                    )
                    .order_by(outcome_window_reports.c.window_end.desc())
                    .limit(self._config.governance.maximum_metric_windows)
                ).scalars()
            )
            cycle_counts = tuple(
                connection.execute(
                    select(analysis_cycles.c.outcome, func.count())
                    .where(
                        analysis_cycles.c.as_of >= cutoff,
                        analysis_cycles.c.as_of <= as_of,
                    )
                    .group_by(analysis_cycles.c.outcome)
                    .order_by(analysis_cycles.c.outcome)
                ).all()
            )
            reconciliation_counts = tuple(
                connection.execute(
                    select(reconciliation_reports.c.status, func.count())
                    .where(
                        reconciliation_reports.c.as_of >= cutoff,
                        reconciliation_reports.c.as_of <= as_of,
                    )
                    .group_by(reconciliation_reports.c.status)
                    .order_by(reconciliation_reports.c.status)
                ).all()
            )
            codex_counts = tuple(
                connection.execute(
                    select(codex_runs.c.status, func.count())
                    .where(
                        codex_runs.c.cycle_id.in_(
                            select(analysis_cycles.c.cycle_id).where(
                                analysis_cycles.c.as_of >= cutoff,
                                analysis_cycles.c.as_of <= as_of,
                            )
                        )
                    )
                    .group_by(codex_runs.c.status)
                    .order_by(codex_runs.c.status)
                ).all()
            )
            trigger_plan_payloads = tuple(
                connection.execute(
                    select(analysis_trigger_plans.c.payload)
                    .where(
                        analysis_trigger_plans.c.is_current.is_(True),
                        analysis_trigger_plans.c.manifest_id == champion.manifest_id,
                    )
                    .order_by(
                        analysis_trigger_plans.c.symbol,
                        analysis_trigger_plans.c.pipeline_id,
                    )
                ).scalars()
            )
            trigger_outbox_counts = tuple(
                connection.execute(
                    select(trigger_outbox.c.status, func.count())
                    .where(trigger_outbox.c.created_at >= cutoff)
                    .group_by(trigger_outbox.c.status)
                    .order_by(trigger_outbox.c.status)
                ).all()
            )

        metric_summaries: list[tuple[str, str]] = []
        for payload in reversed(window_payloads):
            report = OutcomeWindowReport.model_validate(payload)
            prefix = f"outcome_window:{report.window_start.isoformat()}"
            metric_summaries.extend(
                (
                    (f"{prefix}:cycle_count", str(report.cycle_count)),
                    (f"{prefix}:closed_trade_count", str(report.closed_trade_count)),
                    (f"{prefix}:net_pnl", str(report.net_pnl)),
                    (f"{prefix}:total_fees", str(report.total_fees)),
                    (f"{prefix}:maximum_drawdown", str(report.maximum_drawdown)),
                    (
                        f"{prefix}:profit_factor",
                        str(report.profit_factor) if report.profit_factor is not None else "NA",
                    ),
                )
            )
        metric_summaries.extend(
            (f"cycle_outcome:{status}", str(count)) for status, count in cycle_counts
        )
        metric_summaries.extend(
            (f"reconciliation:{status}", str(count)) for status, count in reconciliation_counts
        )
        metric_summaries.extend(
            (f"codex_run:{status}", str(count)) for status, count in codex_counts
        )
        metric_summaries.extend(
            (f"trigger_outbox:{status}", str(count)) for status, count in trigger_outbox_counts
        )
        snapshot = build_governance_snapshot(
            as_of=as_of,
            constitution=constitution,
            champion=champion,
            previous_stable_manifest_ids=previous_stable,
            metric_summaries=tuple(metric_summaries),
            failed_experiments=failed,
            open_proposal_ids=open_proposals,
            available_evaluation_plans=plans,
            architecture_decision_ids=architecture_ids,
            analysis_trigger_plans=tuple(
                AnalysisTriggerPlan.model_validate(payload) for payload in trigger_plan_payloads
            ),
            complexity_used=champion.complexity_score,
            complexity_limit=self._config.governance.complexity_limit,
        )
        self._repository.record_snapshot(snapshot)
        return snapshot
