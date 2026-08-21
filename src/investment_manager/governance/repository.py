from __future__ import annotations

from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.governance.evaluation.performance import ReplayEvaluationReport
from investment_manager.governance.models import (
    BlindEvaluationClaim,
    ChangeProposal,
    EvaluationPlan,
    EvaluationResult,
    EvaluationStage,
    EvaluationTarget,
    FailedExperiment,
    GovernanceSnapshot,
    ReleaseApprovalDecision,
    ReleaseManifest,
    SystemConstitution,
)
from investment_manager.governance.tables import (
    blind_evaluation_claims,
    change_proposals,
    evaluation_plans,
    evaluation_results,
    failed_experiment_records,
    governance_decisions,
    governance_snapshots,
    release_approval_requests,
    release_manifests,
    replay_evaluation_reports,
    system_constitutions,
)
from investment_manager.kernel.identity import content_hash


class SqlGovernanceRepository:
    """主 Agent 的长期记忆只由这些版本化事实构成，不保存聊天上下文。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_constitution(self, constitution: SystemConstitution) -> None:
        self._record(
            system_constitutions,
            system_constitutions.c.version,
            constitution.version,
            {
                "version": constitution.version,
                "payload": constitution.model_dump(mode="json"),
            },
        )

    def record_release(self, release: ReleaseManifest) -> None:
        self._record(
            release_manifests,
            release_manifests.c.manifest_id,
            release.manifest_id,
            {
                "manifest_id": release.manifest_id,
                "content_hash": content_hash(release),
                "status": release.status,
                "payload": release.model_dump(mode="json"),
            },
        )

    def get_champion(self) -> ReleaseManifest:
        """Return the single release authorized as Champion, or fail closed."""

        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(release_manifests.c.payload).where(
                        release_manifests.c.status == "CHAMPION"
                    )
                ).scalars()
            )
        if len(payloads) != 1:
            raise ValueError("治理事实库中必须恰好存在一个 CHAMPION")
        return ReleaseManifest.model_validate(payloads[0])

    def record_snapshot(self, snapshot: GovernanceSnapshot) -> None:
        self._record(
            governance_snapshots,
            governance_snapshots.c.snapshot_id,
            snapshot.snapshot_id,
            {
                "snapshot_id": snapshot.snapshot_id,
                "as_of": snapshot.as_of,
                "champion_manifest_id": snapshot.champion.manifest_id,
                "content_hash": snapshot.content_hash,
                "payload": snapshot.model_dump(mode="json"),
            },
        )

    def get_snapshot(self, snapshot_id: str) -> GovernanceSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(governance_snapshots.c.payload).where(
                    governance_snapshots.c.snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
        return GovernanceSnapshot.model_validate(payload) if payload else None

    def register_plan(self, plan: EvaluationPlan) -> None:
        self._record(
            evaluation_plans,
            evaluation_plans.c.plan_id,
            plan.plan_id,
            {
                "plan_id": plan.plan_id,
                "registered_at": plan.registered_at,
                "base_manifest_id": plan.base_manifest_id,
                "regression_suite_version": plan.fixed_regression_suite_version,
                "payload": plan.model_dump(mode="json"),
            },
        )

    def get_plan(self, plan_id: str) -> EvaluationPlan | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(evaluation_plans.c.payload).where(
                    evaluation_plans.c.plan_id == plan_id
                )
            ).scalar_one_or_none()
        return EvaluationPlan.model_validate(payload) if payload else None

    def plans_for_manifest(self, manifest_id: str) -> tuple[EvaluationPlan, ...]:
        """Return preregistered plans bound to one exact release manifest."""

        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(evaluation_plans.c.payload)
                    .where(evaluation_plans.c.base_manifest_id == manifest_id)
                    .order_by(
                        evaluation_plans.c.registered_at,
                        evaluation_plans.c.plan_id,
                    )
                ).scalars()
            )
        return tuple(EvaluationPlan.model_validate(payload) for payload in payloads)

    def claim_blind_evaluation(self, claim: BlindEvaluationClaim) -> BlindEvaluationClaim:
        """Atomically consume one plan's blind query budget; exact retries resume."""

        if any(
            item is not None
            for item in (claim.completed_at, claim.result_id, claim.result_hash)
        ):
            raise ValueError("首次认领盲测预算时不能携带完成结果")
        plan = self.get_plan(claim.plan_id)
        if (
            plan is None
            or EvaluationStage.BLIND not in plan.required_stages
            or plan.blind_query_budget != 1
        ):
            raise ValueError("EvaluationPlan 没有可消费的一次性盲测预算")
        values = {
            "plan_id": claim.plan_id,
            "query_id": claim.query_id,
            "blind_scope_id": claim.blind_scope_id,
            "blind_symbol": claim.blind_symbol,
            "blind_start": claim.blind_start,
            "blind_end": claim.blind_end,
            "source_evaluation_id": claim.source_evaluation_id,
            "claimed_at": claim.claimed_at,
            "payload": claim.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                existing_payload = connection.execute(
                    select(blind_evaluation_claims.c.payload).where(
                        blind_evaluation_claims.c.plan_id == claim.plan_id
                    )
                ).scalar_one_or_none()
                if existing_payload is not None:
                    existing = BlindEvaluationClaim.model_validate(existing_payload)
                    if (
                        existing.query_id != claim.query_id
                        or existing.blind_scope_id != claim.blind_scope_id
                        or existing.source_evaluation_id != claim.source_evaluation_id
                    ):
                        raise ValueError("EvaluationPlan 的盲测查询预算已经被消费")
                    return existing
                if connection.dialect.name == "postgresql":
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                        {"lock_key": f"blind-evaluation:{claim.blind_symbol}"},
                    )
                overlap = connection.execute(
                    select(blind_evaluation_claims.c.plan_id).where(
                        blind_evaluation_claims.c.blind_symbol == claim.blind_symbol,
                        blind_evaluation_claims.c.blind_start < claim.blind_end,
                        blind_evaluation_claims.c.blind_end > claim.blind_start,
                    )
                ).scalar_one_or_none()
                if overlap is not None:
                    raise ValueError("该品种的盲测时间窗已被揭示或与其重叠")
                connection.execute(insert(blind_evaluation_claims).values(**values))
            return claim
        except IntegrityError:
            existing = self.get_blind_evaluation_claim(claim.plan_id)
            if existing is None or (
                existing.query_id != claim.query_id
                or existing.blind_scope_id != claim.blind_scope_id
                or existing.source_evaluation_id != claim.source_evaluation_id
            ):
                raise ValueError("EvaluationPlan 或盲测时间窗已经被消费") from None
            return existing

    def get_blind_evaluation_claim(self, plan_id: str) -> BlindEvaluationClaim | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(blind_evaluation_claims.c.payload).where(
                    blind_evaluation_claims.c.plan_id == plan_id
                )
            ).scalar_one_or_none()
        return BlindEvaluationClaim.model_validate(payload) if payload else None

    def complete_blind_evaluation(self, completed: BlindEvaluationClaim) -> BlindEvaluationClaim:
        """Complete a claimed reveal once; the identical completion is idempotent."""

        if any(
            item is None
            for item in (completed.completed_at, completed.result_id, completed.result_hash)
        ):
            raise ValueError("完成盲测必须携带结果身份、哈希与完成时间")
        with self._engine.begin() as connection:
            payload = connection.execute(
                select(blind_evaluation_claims.c.payload)
                .where(blind_evaluation_claims.c.plan_id == completed.plan_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                raise ValueError("盲测预算尚未认领")
            existing = BlindEvaluationClaim.model_validate(payload)
            if (
                existing.query_id != completed.query_id
                or existing.source_evaluation_id != completed.source_evaluation_id
                or existing.claimed_at != completed.claimed_at
            ):
                raise ValueError("盲测完成事实与原始认领不一致")
            if existing.completed_at is not None:
                if existing != completed:
                    raise ValueError("同一次盲测已经登记了不同结果")
                return existing
            connection.execute(
                update(blind_evaluation_claims)
                .where(blind_evaluation_claims.c.plan_id == completed.plan_id)
                .values(
                    completed_at=completed.completed_at,
                    result_id=completed.result_id,
                    result_hash=completed.result_hash,
                    payload=completed.model_dump(mode="json"),
                )
            )
        return completed

    def register_proposal(self, proposal: ChangeProposal) -> None:
        self._record(
            change_proposals,
            change_proposals.c.proposal_id,
            proposal.proposal_id,
            {
                "proposal_id": proposal.proposal_id,
                "base_version": proposal.base_manifest_id,
                "change_type": proposal.change_type.value,
                "status": "PROPOSED",
                "payload": proposal.model_dump(mode="json"),
            },
        )

    def record_evaluation(self, result: EvaluationResult) -> None:
        self._record(
            evaluation_results,
            evaluation_results.c.evaluation_id,
            result.evaluation_id,
            {
                "evaluation_id": result.evaluation_id,
                "proposal_id": result.proposal_id,
                "plan_id": result.plan_id,
                "candidate_manifest_id": result.candidate_manifest_id,
                "completed_at": result.completed_at,
                "payload": result.model_dump(mode="json"),
            },
        )

    def require_registered_evaluation_target(
        self,
        target: EvaluationTarget,
        *,
        require_current_base: bool = True,
    ) -> None:
        """拒绝由调用方临时拼出的提案、计划或候选版本。"""
        with self._engine.connect() as connection:
            plan_payload = connection.execute(
                select(evaluation_plans.c.payload).where(
                    evaluation_plans.c.plan_id == target.plan.plan_id
                )
            ).scalar_one_or_none()
            proposal_row = connection.execute(
                select(
                    change_proposals.c.status,
                    change_proposals.c.payload,
                    governance_decisions.c.status,
                    governance_decisions.c.payload,
                )
                .join(
                    governance_decisions,
                    governance_decisions.c.decision_id == change_proposals.c.proposal_id,
                )
                .where(change_proposals.c.proposal_id == target.proposal.proposal_id)
            ).one_or_none()
            candidate_row = connection.execute(
                select(release_manifests.c.status, release_manifests.c.payload).where(
                    release_manifests.c.manifest_id == target.candidate.manifest_id
                )
            ).one_or_none()
            champion_row = connection.execute(
                select(release_manifests.c.status).where(
                    release_manifests.c.manifest_id == target.plan.base_manifest_id
                )
            ).one_or_none()
        if plan_payload != target.plan.model_dump(mode="json"):
            raise ValueError("EvaluationPlan 未登记或登记内容不一致")
        expected_proposal = target.proposal.model_dump(mode="json")
        if (
            proposal_row is None
            or proposal_row[0] != "PROPOSED"
            or proposal_row[1] != expected_proposal
            or proposal_row[2] != "PROPOSED"
            or proposal_row[3] != expected_proposal
        ):
            raise ValueError("ChangeProposal 没有通过治理门禁并登记")
        if (
            candidate_row is None
            or candidate_row[0] != "CHALLENGER"
            or candidate_row[1] != target.candidate.model_dump(mode="json")
        ):
            raise ValueError("候选 ReleaseManifest 未登记或内容不一致")
        allowed_base_statuses = (
            {"CHAMPION"} if require_current_base else {"CHAMPION", "PREVIOUS_STABLE"}
        )
        if champion_row is None or champion_row[0] not in allowed_base_statuses:
            raise ValueError("评估计划的基线不是已登记稳定版本")

    def require_registered_release_inputs(
        self,
        *,
        target: EvaluationTarget,
        evaluation: EvaluationResult,
        current_champion: ReleaseManifest,
    ) -> None:
        self.require_registered_evaluation_target(target, require_current_base=False)
        with self._engine.connect() as connection:
            evaluation_payload = connection.execute(
                select(evaluation_results.c.payload).where(
                    evaluation_results.c.evaluation_id == evaluation.evaluation_id
                )
            ).scalar_one_or_none()
            champion_payloads = tuple(
                connection.execute(
                    select(release_manifests.c.payload).where(
                        release_manifests.c.status == "CHAMPION"
                    )
                ).scalars()
            )
        if evaluation_payload != evaluation.model_dump(mode="json"):
            raise ValueError("EvaluationResult 未登记或登记内容不一致")
        if champion_payloads != (current_champion.model_dump(mode="json"),):
            raise ValueError("ReleaseWorkflow 指定的当前 CHAMPION 不权威")

    def record_release_approval(self, decision: ReleaseApprovalDecision) -> None:
        values = {
            "decision_id": decision.decision_id,
            "evaluation_id": decision.evaluation_id,
            "candidate_manifest_id": decision.candidate_manifest_id,
            "status": decision.status.value,
            "created_at": decision.created_at,
            "payload": decision.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(release_approval_requests).values(**values))
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(release_approval_requests.c.payload).where(
                        release_approval_requests.c.evaluation_id == decision.evaluation_id
                    )
                ).scalar_one_or_none()
            if existing != values["payload"]:
                raise ValueError("同一 EvaluationResult 已存在不同的 Release 决策") from None

    def record_failed_experiment(self, failed: FailedExperiment) -> None:
        values = {
            "experiment_id": failed.experiment_id,
            "hypothesis_fingerprint": failed.hypothesis_fingerprint,
            "rejected_at": failed.rejected_at,
            "payload": failed.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(failed_experiment_records).values(**values))
        except IntegrityError:
            with self._engine.connect() as connection:
                payload = connection.execute(
                    select(failed_experiment_records.c.payload).where(
                        failed_experiment_records.c.experiment_id == failed.experiment_id
                    )
                ).scalar_one()
            existing = FailedExperiment.model_validate(payload)
            if existing.model_copy(update={"rejected_at": failed.rejected_at}) != failed:
                raise ValueError(
                    f"治理事实 {failed.experiment_id} 已存在且内容不同"
                ) from None

    def get_failed_experiment(self, experiment_id: str) -> FailedExperiment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(failed_experiment_records.c.payload).where(
                    failed_experiment_records.c.experiment_id == experiment_id
                )
            ).scalar_one_or_none()
        return FailedExperiment.model_validate(payload) if payload else None

    def _record(self, table, key_column, key: str, values: dict) -> None:
        payload = values["payload"]
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(table).values(**values))
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(table.c.payload).where(key_column == key)
                ).scalar_one()
            if existing != payload:
                raise ValueError(f"治理事实 {key} 已存在且内容不同") from None


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
