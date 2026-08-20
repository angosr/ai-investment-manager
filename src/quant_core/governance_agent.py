from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from quant_core.analyst import (
    CodexAccountRouter,
    RunBundle,
    verify_bundle,
    write_run_bundle,
)
from quant_core.config import CodexRuntimePolicy
from quant_core.domain import FrozenModel
from quant_core.governance import (
    ChangeProposal,
    GovernanceGate,
    GovernanceSnapshot,
    NoChange,
)
from quant_core.ids import canonical_json, content_hash, stable_id
from quant_core.persistence import change_proposals, governance_decisions
from quant_core.trigger import AnalysisTriggerPlan, TriggerPlanPatch

GovernorDecision = ChangeProposal | NoChange
GOVERNOR_DECISION_ADAPTER = TypeAdapter(GovernorDecision)


class GovernorOutput(FrozenModel):
    decision: GovernorDecision
    trigger_plan_patch: TriggerPlanPatch | None = None


GOVERNOR_OUTPUT_ADAPTER = TypeAdapter(GovernorOutput)


@dataclass(frozen=True, slots=True)
class GovernorRunResult:
    success: bool
    reason_code: str
    decision: GovernorDecision | None = None
    trigger_plan_patch: TriggerPlanPatch | None = None
    applied_trigger_plan: AnalysisTriggerPlan | None = None
    account_id: str | None = None
    attempts: int = 0


class Governor(Protocol):
    def govern(self, snapshot: GovernanceSnapshot) -> GovernorRunResult: ...


class TriggerPlanStore(Protocol):
    def apply_patch(
        self,
        patch: TriggerPlanPatch,
        *,
        now,
        current_manifest_id: str,
    ): ...


class GovernorBundleBuilder:
    def __init__(
        self,
        runtime: CodexRuntimePolicy,
        *,
        prompt_path: Path,
        code_version: str = "working-tree",
    ) -> None:
        self._runtime = runtime
        self._prompt_path = prompt_path
        self._code_version = code_version

    def build(self, snapshot: GovernanceSnapshot, target: Path) -> RunBundle:
        base_prompt = self._prompt_path.read_text(encoding="utf-8").strip()
        snapshot_json = canonical_json(snapshot)
        prompt = (
            base_prompt + "\n\n所需治理快照已完整内嵌在本提示中；禁止调用任何工具，"
            "禁止访问文件系统或网络。只允许引用内嵌 governance_snapshot_json 中的"
            " evidence ID 和"
            " available_evaluation_plans。created_at 必须等于快照 as_of。"
            "failed_experiments 是已经证伪的负面知识，历史研究失败的首个 evidence_id"
            " 以 hypothesis: 开头描述规范假设；若没有新的可见证据和明确的结构差异，"
            "不得改写 hypothesis 措辞后重复提出。"
            "若没有可用评估计划、已有未结提案或证据不足，decision 输出 NO_CHANGE。"
            "trigger_plan_patch 是可选项，只能引用快照中的当前 AnalysisTriggerPlan；"
            "它可以单独调整 AI 分析触发，但不能修改生产策略或风控。"
            "\n\n<governance_snapshot_json>\n"
            f"{snapshot_json}\n"
            "</governance_snapshot_json>"
        )
        if len(prompt) > self._runtime.maximum_prompt_characters:
            raise ValueError("Governor 内嵌治理快照超过 Codex 提示容量上限")
        files = {
            "governance_snapshot.json": snapshot_json + "\n",
            "governor_prompt.md": prompt + "\n",
            "output.schema.json": json.dumps(
                GOVERNOR_OUTPUT_ADAPTER.json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        }
        return write_run_bundle(
            cycle_id=snapshot.snapshot_id,
            target=target,
            prompt=prompt,
            files=files,
            manifest={
                "role": "GOVERNOR",
                "snapshot_hash": snapshot.content_hash,
                "governance_policy_version": snapshot.champion.component_versions,
                "runtime_policy_version": self._runtime.version,
                "model": self._runtime.model,
                "reasoning_effort": self._runtime.reasoning_effort,
                "code_version": self._code_version,
            },
        )


class SqlGovernorDecisionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        snapshot: GovernanceSnapshot,
        decision: GovernorDecision,
    ) -> GovernorDecision:
        decision_id = (
            decision.proposal_id if isinstance(decision, ChangeProposal) else decision.decision_id
        )
        payload = decision.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                existing_payload = connection.execute(
                    select(governance_decisions.c.payload).where(
                        governance_decisions.c.snapshot_id == snapshot.snapshot_id
                    )
                ).scalar_one_or_none()
                if existing_payload is not None:
                    existing = GOVERNOR_DECISION_ADAPTER.validate_python(existing_payload)
                    if existing != decision:
                        raise ValueError("同一治理快照已存在不同决策")
                    return existing
                if isinstance(decision, ChangeProposal):
                    connection.execute(
                        insert(change_proposals).values(
                            proposal_id=decision.proposal_id,
                            base_version=decision.base_manifest_id,
                            change_type=decision.change_type.value,
                            status="PROPOSED",
                            payload=payload,
                        )
                    )
                connection.execute(
                    insert(governance_decisions).values(
                        decision_id=decision_id,
                        snapshot_id=snapshot.snapshot_id,
                        decision_type=decision.decision_type,
                        status=(
                            "PROPOSED" if isinstance(decision, ChangeProposal) else "NO_CHANGE"
                        ),
                        payload=payload,
                    )
                )
        except IntegrityError:
            with self._engine.connect() as connection:
                existing_payload = connection.execute(
                    select(governance_decisions.c.payload).where(
                        governance_decisions.c.snapshot_id == snapshot.snapshot_id
                    )
                ).scalar_one_or_none()
            if existing_payload is None:
                raise
            existing = GOVERNOR_DECISION_ADAPTER.validate_python(existing_payload)
            if existing != decision:
                raise ValueError("并发治理决策不一致") from None
            return existing
        return decision


class CodexGovernor:
    def __init__(
        self,
        *,
        bundle_root: Path,
        bundle_builder: GovernorBundleBuilder,
        router: CodexAccountRouter,
        decisions: SqlGovernorDecisionStore,
        trigger_plans: TriggerPlanStore | None = None,
    ) -> None:
        self._bundle_root = bundle_root
        self._bundle_builder = bundle_builder
        self._router = router
        self._decisions = decisions
        self._trigger_plans = trigger_plans

    def govern(self, snapshot: GovernanceSnapshot) -> GovernorRunResult:
        if snapshot.open_proposal_ids and not snapshot.analysis_trigger_plans:
            return self._record_no_change(snapshot, "OPEN_PROPOSAL_ALREADY_EXISTS")
        if not snapshot.available_evaluation_plans and not snapshot.analysis_trigger_plans:
            return self._record_no_change(snapshot, "NO_PREREGISTERED_EVALUATION_PLAN")
        target = (
            self._bundle_root
            / "governance"
            / stable_id("bundle", snapshot.snapshot_id, snapshot.content_hash)
        )
        try:
            bundle = self._load_existing(snapshot.snapshot_id, target)
            if bundle is None:
                bundle = self._bundle_builder.build(snapshot, target)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return GovernorRunResult(False, "GOVERNOR_BUNDLE_INVALID")
        routed = self._router.run(bundle)
        if not routed.success or routed.output is None:
            return GovernorRunResult(
                False,
                routed.reason_code,
                account_id=routed.account_id,
                attempts=routed.attempts,
            )
        try:
            raw_output = GOVERNOR_OUTPUT_ADAPTER.validate_python(routed.output)
            decision = self._normalize(snapshot, raw_output.decision)
            self._validate(snapshot, decision)
            applied_plan = self._apply_trigger_patch(snapshot, raw_output.trigger_plan_patch)
            stored = self._decisions.record(snapshot, decision)
        except ValueError:
            return GovernorRunResult(
                False,
                "GOVERNOR_DETERMINISTIC_VALIDATION",
                account_id=routed.account_id,
                attempts=routed.attempts,
            )
        return GovernorRunResult(
            True,
            "GOVERNOR_DECISION_RECORDED",
            decision=stored,
            trigger_plan_patch=raw_output.trigger_plan_patch,
            applied_trigger_plan=applied_plan,
            account_id=routed.account_id,
            attempts=routed.attempts,
        )

    def _record_no_change(
        self,
        snapshot: GovernanceSnapshot,
        reason_code: str,
    ) -> GovernorRunResult:
        decision = NoChange(
            decision_id=stable_id("no_change", snapshot.snapshot_id, reason_code),
            observed_at=snapshot.as_of,
            reason_codes=(reason_code,),
            revisit_conditions=("NEW_COMPLETE_OUTCOME_WINDOW_OR_PREREGISTERED_PLAN",),
        )
        return GovernorRunResult(
            True,
            "GOVERNOR_DECISION_RECORDED",
            decision=self._decisions.record(snapshot, decision),
        )

    @staticmethod
    def _normalize(
        snapshot: GovernanceSnapshot,
        decision: GovernorDecision,
    ) -> GovernorDecision:
        if isinstance(decision, NoChange):
            payload_hash = content_hash(
                decision.model_dump(
                    mode="json",
                    exclude={"decision_id", "observed_at"},
                )
            )
            return decision.model_copy(
                update={
                    "decision_id": stable_id("no_change", snapshot.snapshot_id, payload_hash),
                    "observed_at": snapshot.as_of,
                }
            )
        payload_hash = content_hash(
            decision.model_dump(mode="json", exclude={"proposal_id", "created_at"})
        )
        return decision.model_copy(
            update={
                "proposal_id": stable_id("change", snapshot.snapshot_id, payload_hash),
                "created_at": snapshot.as_of,
            }
        )

    @staticmethod
    def _validate(snapshot: GovernanceSnapshot, decision: GovernorDecision) -> None:
        if isinstance(decision, NoChange):
            return
        plans = {item.plan_id: item for item in snapshot.available_evaluation_plans}
        plan = plans.get(decision.evaluation_plan_id)
        if plan is None:
            raise ValueError("Governor 引用了未预登记的 EvaluationPlan")
        known_evidence = {
            *(item.experiment_id for item in snapshot.failed_experiments),
            *snapshot.architecture_decision_ids,
            *(key for key, _ in snapshot.metric_summaries),
        }
        if not set(decision.evidence_ids).issubset(known_evidence):
            raise ValueError("Governor 引用了治理快照之外的证据")
        result = GovernanceGate().validate(decision, plan, snapshot)
        if not result.accepted:
            raise ValueError("Governor 提案未通过确定性治理门禁")

    def _apply_trigger_patch(
        self,
        snapshot: GovernanceSnapshot,
        patch: TriggerPlanPatch | None,
    ) -> AnalysisTriggerPlan | None:
        if patch is None:
            return None
        plans = {item.plan_id: item for item in snapshot.analysis_trigger_plans}
        current = plans.get(patch.plan_id)
        if current is None or patch.expected_revision != current.revision:
            raise ValueError("Governor TriggerPlanPatch 未引用快照中的当前 revision")
        if patch.manifest_id != snapshot.champion.manifest_id:
            raise ValueError("Governor TriggerPlanPatch 不属于当前 Champion")
        if self._trigger_plans is None:
            raise ValueError("Governor 未装配 TriggerPlanStore")
        result = self._trigger_plans.apply_patch(
            patch,
            now=snapshot.as_of,
            current_manifest_id=snapshot.champion.manifest_id,
        )
        return result.plan

    @staticmethod
    def _load_existing(snapshot_id: str, target: Path) -> RunBundle | None:
        if not target.exists():
            return None
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        bundle = RunBundle(
            cycle_id=snapshot_id,
            path=target,
            bundle_hash=content_hash({"manifest": manifest}),
            prompt=(target / "governor_prompt.md").read_text(encoding="utf-8").strip(),
        )
        if not verify_bundle(bundle):
            raise ValueError("已有 Governor 运行包校验失败")
        return bundle
