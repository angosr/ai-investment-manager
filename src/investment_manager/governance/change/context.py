from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from investment_manager.execution.tables import reconciliation_reports
from investment_manager.forecast.tables import codex_runs
from investment_manager.governance.evaluation.performance import OutcomeWindowReport
from investment_manager.governance.models import (
    EvaluationPlan,
    FailedExperiment,
    GovernanceSnapshot,
    build_governance_snapshot,
    load_constitution,
    load_release_manifest,
    validate_manifest_against_config,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.governance.tables import (
    architecture_decisions,
    change_proposals,
    evaluation_plans,
    failed_experiment_records,
    outcome_window_reports,
    release_manifests,
)
from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.tables import normalized_events
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.legacy.models import (
    CandidateOutcome,
    CandidateOutcomeStatus,
)
from investment_manager.legacy.repository import (
    analysis_cycles,
    candidate_outcomes,
    signal_candidates,
)
from investment_manager.platform.time import database_utc
from investment_manager.scheduling.models import (
    AnalysisTriggerPlan,
    AnalysisTriggerType,
    TriggerBatch,
)
from investment_manager.scheduling.tables import (
    analysis_trigger_batches,
    analysis_trigger_plans,
    trigger_outbox,
)
from investment_manager.settings import AppConfig


@dataclass(slots=True)
class _CodexRuntimeStats:
    statuses: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    durations: list[Decimal] = field(default_factory=list)


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
        as_of = require_utc(as_of)
        constitution = load_constitution(self._root / "config" / "system-constitution.yaml")
        bootstrap = load_release_manifest(self._root / "config" / "release-manifest.yaml")
        validate_manifest_against_config(bootstrap, self._config)
        if bootstrap.constitution_version != constitution.version:
            raise ValueError("ReleaseManifest 与系统宪法版本不一致")
        self._repository.record_constitution(constitution)
        self._repository.record_release(bootstrap)

        cutoff = as_of - timedelta(days=self._config.governance.snapshot_lookback_days)
        with self._engine.connect() as connection:
            champion = self._repository.get_champion()
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
            codex_rows = tuple(
                connection.execute(
                    select(
                        analysis_cycles.c.pipeline_version,
                        codex_runs.c.status,
                        codex_runs.c.error_class,
                        codex_runs.c.payload,
                    )
                    .select_from(
                        codex_runs.join(
                            analysis_cycles,
                            analysis_cycles.c.cycle_id == codex_runs.c.cycle_id,
                        )
                    )
                    .where(
                        analysis_cycles.c.as_of >= cutoff,
                        analysis_cycles.c.as_of <= as_of,
                    )
                    .order_by(analysis_cycles.c.pipeline_version, codex_runs.c.run_id)
                ).mappings()
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
            trigger_batch_rows = tuple(
                connection.execute(
                    select(
                        analysis_trigger_batches.c.batch_id,
                        analysis_trigger_batches.c.analysis_submitted_at,
                        analysis_trigger_batches.c.payload,
                    )
                    .where(
                        analysis_trigger_batches.c.pipeline_id == self._config.pipeline.version,
                        analysis_trigger_batches.c.symbol.in_(self._config.market_data.symbols),
                        analysis_trigger_batches.c.analysis_submitted_at >= cutoff,
                        analysis_trigger_batches.c.analysis_submitted_at <= as_of,
                    )
                    .order_by(analysis_trigger_batches.c.analysis_submitted_at)
                ).mappings()
            )
            cycle_created_at_by_id = dict(
                connection.execute(
                    select(analysis_cycles.c.cycle_id, analysis_cycles.c.created_at).where(
                        analysis_cycles.c.pipeline_version == self._config.pipeline.version,
                        analysis_cycles.c.as_of >= cutoff,
                        analysis_cycles.c.as_of <= as_of,
                        analysis_cycles.c.created_at <= as_of,
                    )
                ).all()
            )
            intelligence_payloads = tuple(
                connection.execute(
                    select(normalized_events.c.payload)
                    .where(
                        normalized_events.c.observed_at >= cutoff,
                        normalized_events.c.observed_at <= as_of,
                    )
                    .order_by(
                        normalized_events.c.observed_at,
                        normalized_events.c.evidence_id,
                    )
                ).scalars()
            )
            candidate_sample_counts = tuple(
                connection.execute(
                    select(
                        signal_candidates.c.producer_id,
                        signal_candidates.c.producer_version,
                        signal_candidates.c.symbol,
                        func.count(signal_candidates.c.candidate_id),
                        func.count(candidate_outcomes.c.outcome_id).filter(
                            candidate_outcomes.c.status == CandidateOutcomeStatus.SETTLED.value
                        ),
                        func.count(candidate_outcomes.c.outcome_id).filter(
                            candidate_outcomes.c.status == CandidateOutcomeStatus.UNSCORABLE.value
                        ),
                    )
                    .select_from(
                        signal_candidates.join(
                            analysis_cycles,
                            analysis_cycles.c.cycle_id == signal_candidates.c.cycle_id,
                        ).outerjoin(
                            candidate_outcomes,
                            and_(
                                candidate_outcomes.c.candidate_id
                                == signal_candidates.c.candidate_id,
                                candidate_outcomes.c.settled_at <= as_of,
                            ),
                        )
                    )
                    .where(
                        analysis_cycles.c.as_of >= cutoff,
                        analysis_cycles.c.as_of <= as_of,
                    )
                    .group_by(
                        signal_candidates.c.producer_id,
                        signal_candidates.c.producer_version,
                        signal_candidates.c.symbol,
                    )
                    .order_by(
                        signal_candidates.c.producer_id,
                        signal_candidates.c.producer_version,
                        signal_candidates.c.symbol,
                    )
                ).all()
            )
            candidate_evidence_stats: dict[
                tuple[str, str, str, str, int, str, str, str, str, str],
                tuple[int, Decimal | None, Decimal | None, Decimal | None],
            ] = {}
            non_overlapping_settled_counts: dict[
                tuple[str, str, str, str, int, str, str, str, str], int
            ] = {}
            last_evaluation_by_sample: dict[
                tuple[str, str, str, str, int, str, str, str, str], datetime
            ] = {}
            outcome_payloads = connection.execute(
                select(candidate_outcomes.c.payload)
                .where(
                    candidate_outcomes.c.evaluation_at >= cutoff,
                    candidate_outcomes.c.evaluation_at <= as_of,
                    candidate_outcomes.c.settled_at <= as_of,
                )
                .order_by(
                    candidate_outcomes.c.evaluation_at,
                    candidate_outcomes.c.outcome_id,
                )
            ).scalars()
            for payload in outcome_payloads:
                outcome = CandidateOutcome.model_validate(payload)
                horizon_minutes = int(
                    (outcome.evaluation_at - outcome.signal_observed_at).total_seconds() // 60
                )
                basis_key = (
                    outcome.producer_id,
                    outcome.producer_version,
                    outcome.symbol,
                    outcome.side.value,
                    horizon_minutes,
                    outcome.calibration_ref,
                    outcome.evaluation_version,
                    outcome.execution_policy_version,
                    outcome.frequency_policy_version,
                )
                status_key = (*basis_key, outcome.status.value)
                count, total, minimum, maximum = candidate_evidence_stats.get(
                    status_key, (0, None, None, None)
                )
                net = outcome.net_return_bps
                if net is not None:
                    total = net if total is None else total + net
                    minimum = net if minimum is None else min(minimum, net)
                    maximum = net if maximum is None else max(maximum, net)
                candidate_evidence_stats[status_key] = (
                    count + 1,
                    total,
                    minimum,
                    maximum,
                )
                if outcome.status != CandidateOutcomeStatus.SETTLED:
                    continue
                last_evaluation = last_evaluation_by_sample.get(basis_key)
                if last_evaluation is not None and outcome.signal_observed_at < last_evaluation:
                    continue
                non_overlapping_settled_counts[basis_key] = (
                    non_overlapping_settled_counts.get(basis_key, 0) + 1
                )
                last_evaluation_by_sample[basis_key] = outcome.evaluation_at

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
        metric_summaries.extend(_codex_run_summaries(codex_rows, as_of=as_of))
        metric_summaries.extend(
            (f"trigger_outbox:{status}", str(count)) for status, count in trigger_outbox_counts
        )
        metric_summaries.extend(
            _trigger_latency_summaries(trigger_batch_rows, cycle_created_at_by_id)
        )
        metric_summaries.extend(
            _intelligence_event_summaries(
                intelligence_payloads,
                high_impact_threshold=self._config.trigger.high_impact_threshold,
            )
        )
        for status_key, (count, total, minimum, maximum) in sorted(
            candidate_evidence_stats.items()
        ):
            *basis_key, status = status_key
            prefix = "candidate_evidence:" + ":".join(str(item) for item in basis_key)
            average = total / count if total is not None else None
            metric_summaries.append((f"{prefix}:{status}:count", str(count)))
            if status == CandidateOutcomeStatus.SETTLED.value:
                metric_summaries.append(
                    (
                        f"{prefix}:non_overlapping_settled",
                        str(non_overlapping_settled_counts.get(tuple(basis_key), 0)),
                    )
                )
            if average is not None:
                metric_summaries.extend(
                    (
                        (f"{prefix}:average_net_bps", str(average)),
                        (f"{prefix}:minimum_net_bps", str(minimum)),
                        (f"{prefix}:maximum_net_bps", str(maximum)),
                    )
                )
        for (
            producer_id,
            producer_version,
            symbol,
            total,
            settled,
            unscorable,
        ) in candidate_sample_counts:
            prefix = f"candidate_sample:{producer_id}:{producer_version}:{symbol}"
            metric_summaries.extend(
                (
                    (f"{prefix}:total", str(total)),
                    (f"{prefix}:settled", str(settled)),
                    (f"{prefix}:unscorable", str(unscorable)),
                    (f"{prefix}:pending", str(total - settled - unscorable)),
                )
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


def _trigger_latency_summaries(
    batch_rows,
    cycle_created_at_by_id: dict[str, datetime],
) -> tuple[tuple[str, str], ...]:
    segments: dict[tuple[str, str, str], dict[str, list[Decimal]]] = {}
    validity: dict[tuple[str, str, str], list[int]] = {}
    for row in batch_rows:
        batch = TriggerBatch.model_validate(row["payload"])
        submitted_at = database_utc(row["analysis_submitted_at"])
        raw_cycle_created_at = cycle_created_at_by_id.get(
            stable_id("triggered_cycle", batch.batch_id)
        )
        cycle_created_at = (
            database_utc(raw_cycle_created_at) if raw_cycle_created_at is not None else None
        )
        earliest_by_type: dict[AnalysisTriggerType, tuple[datetime, datetime]] = {}
        for item in batch.triggers:
            current = earliest_by_type.get(item.trigger_type)
            if current is None:
                earliest_by_type[item.trigger_type] = (item.occurred_at, item.observed_at)
            else:
                earliest_by_type[item.trigger_type] = (
                    min(current[0], item.occurred_at),
                    min(current[1], item.observed_at),
                )
        for trigger_type in sorted(earliest_by_type):
            occurred_at, observed_at = earliest_by_type[trigger_type]
            key = (batch.pipeline_id, batch.symbol, trigger_type.value)
            values = segments.setdefault(
                key,
                {
                    "source_to_observed": [],
                    "observed_to_batch": [],
                    "batch_to_submit": [],
                    "submit_to_decision": [],
                    "source_to_decision": [],
                },
            )
            values["source_to_observed"].append(_duration_seconds(observed_at, occurred_at))
            values["observed_to_batch"].append(_duration_seconds(batch.created_at, observed_at))
            values["batch_to_submit"].append(_duration_seconds(submitted_at, batch.created_at))
            valid, invalid, pending = validity.setdefault(key, [0, 0, 0])
            if cycle_created_at is None:
                validity[key] = [valid, invalid, pending + 1]
                continue
            if cycle_created_at < submitted_at:
                validity[key] = [valid, invalid + 1, pending]
                continue
            validity[key] = [valid + 1, invalid, pending]
            values["submit_to_decision"].append(_duration_seconds(cycle_created_at, submitted_at))
            values["source_to_decision"].append(_duration_seconds(cycle_created_at, occurred_at))

    summaries: list[tuple[str, str]] = []
    for key, values in sorted(segments.items()):
        prefix = "trigger_latency:" + ":".join(key)
        valid, invalid, pending = validity[key]
        summaries.extend(
            (
                (f"{prefix}:sample_count", str(len(values["source_to_observed"]))),
                (f"{prefix}:valid_decision_sample_count", str(valid)),
                (f"{prefix}:invalid_decision_sample_count", str(invalid)),
                (f"{prefix}:pending_decision_sample_count", str(pending)),
            )
        )
        for segment, observations in values.items():
            if not observations:
                continue
            for label, percentile in (
                ("p50_seconds", Decimal("0.50")),
                ("p95_seconds", Decimal("0.95")),
                ("p99_seconds", Decimal("0.99")),
            ):
                summaries.append(
                    (
                        f"{prefix}:{segment}:{label}",
                        _format_decimal(_percentile(observations, percentile)),
                    )
                )
    return tuple(summaries)


def _codex_run_summaries(rows, *, as_of: datetime) -> tuple[tuple[str, str], ...]:
    """按实际尝试耗时汇总 Codex 运行；旧事实缺耗时时只参与计数。"""

    as_of = require_utc(as_of)
    global_counts: Counter[str] = Counter()
    groups: dict[tuple[str, str], _CodexRuntimeStats] = {}
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        raw_observed_at = payload.get("observed_at")
        if not isinstance(raw_observed_at, str):
            continue
        observed_at = require_utc(datetime.fromisoformat(raw_observed_at))
        raw_completed_at = payload.get("completed_at")
        completed_at = (
            require_utc(datetime.fromisoformat(raw_completed_at))
            if isinstance(raw_completed_at, str)
            else observed_at
        )
        if observed_at > as_of or completed_at > as_of:
            continue

        status = str(row["status"])
        error_class = str(row["error_class"] or "NONE")
        runtime_version = str(payload.get("runtime_policy_version") or "legacy-unknown")
        key = (str(row["pipeline_version"]), runtime_version)
        group = groups.setdefault(key, _CodexRuntimeStats())
        global_counts[status] += 1
        group.statuses[status] += 1
        if error_class != "NONE":
            group.failures[error_class] += 1
        duration_ms = payload.get("duration_ms")
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms >= 0:
            group.durations.append(Decimal(duration_ms) / Decimal(1000))

    summaries: list[tuple[str, str]] = [
        (f"codex_run:{status}", str(count)) for status, count in sorted(global_counts.items())
    ]
    for (pipeline_version, runtime_version), group in sorted(groups.items()):
        prefix = f"codex_runtime:{pipeline_version}:{runtime_version}"
        statuses = group.statuses
        failures = group.failures
        durations = group.durations
        summaries.extend(
            (
                (f"{prefix}:total", str(sum(statuses.values()))),
                (f"{prefix}:succeeded", str(statuses["SUCCEEDED"])),
                (f"{prefix}:failed", str(statuses["FAILED"])),
                (f"{prefix}:duration_sample_count", str(len(durations))),
            )
        )
        for failure, count in sorted(failures.items()):
            summaries.append((f"{prefix}:failure:{failure}", str(count)))
        for label, percentile in (
            ("p50_seconds", Decimal("0.50")),
            ("p95_seconds", Decimal("0.95")),
            ("p99_seconds", Decimal("0.99")),
        ):
            if durations:
                summaries.append(
                    (
                        f"{prefix}:duration:{label}",
                        _format_decimal(_percentile(durations, percentile)),
                    )
                )
    return tuple(summaries)


def _intelligence_event_summaries(
    payloads,
    *,
    high_impact_threshold: Decimal,
) -> tuple[tuple[str, str], ...]:
    stats: dict[tuple[str, str], tuple[int, int, Decimal]] = {}
    route_latencies: dict[tuple[str, str, str], list[Decimal]] = {}
    for payload in payloads:
        event = IntelligenceEvent.model_validate(payload)
        key = (event.normalizer_version, event.source)
        count, high_impact_count, total_impact = stats.get(key, (0, 0, Decimal("0")))
        stats[key] = (
            count + 1,
            high_impact_count + int(event.impact >= high_impact_threshold),
            total_impact + event.impact,
        )
        route_key = (event.normalizer_version, event.acquisition_route, event.source)
        route_latencies.setdefault(route_key, []).append(
            _duration_seconds(event.observed_at, event.event_time)
        )

    summaries: list[tuple[str, str]] = []
    for key, (count, high_impact_count, total_impact) in sorted(stats.items()):
        prefix = "intelligence_event:" + ":".join(key)
        summaries.extend(
            (
                (f"{prefix}:count", str(count)),
                (f"{prefix}:high_impact_count", str(high_impact_count)),
                (
                    f"{prefix}:high_impact_rate",
                    _format_decimal(Decimal(high_impact_count) / Decimal(count)),
                ),
                (
                    f"{prefix}:average_impact",
                    _format_decimal(total_impact / Decimal(count)),
                ),
            )
        )
    for key, discovery_latencies in sorted(route_latencies.items()):
        prefix = "intelligence_discovery:" + ":".join(key)
        for label, percentile in (
            ("p50_seconds", Decimal("0.50")),
            ("p95_seconds", Decimal("0.95")),
            ("p99_seconds", Decimal("0.99")),
        ):
            summaries.append(
                (
                    f"{prefix}:{label}",
                    _format_decimal(_percentile(discovery_latencies, percentile)),
                )
            )
    return tuple(summaries)


def _duration_seconds(later: datetime, earlier: datetime) -> Decimal:
    return Decimal(str((require_utc(later) - require_utc(earlier)).total_seconds()))


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal:
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (Decimal("1") - weight) + ordered[upper] * weight


def _format_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")
