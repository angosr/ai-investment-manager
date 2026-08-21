from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from pydantic import Field

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.walk_forward import BlindEvaluationResult, WalkForwardResult


class HistoricalEvaluationEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: WalkForwardResult


class HistoricalExperimentSummary(FrozenModel):
    experiment_id: str
    plan_ids: tuple[str, ...]
    dataset_id: str
    event_dataset_id: str | None = None
    funding_dataset_id: str | None = None
    strategy_family: str | None
    strategy_versions: tuple[str, ...]
    strategy_family_attempt_count: int | None
    attempt_count: int = Field(gt=0)
    evaluation_ids: tuple[str, ...]
    canonical_evaluation_id: str | None
    canonical_backtest_model_version: str | None
    canonical_completed: bool | None
    canonical_passed: bool | None
    superseded_evaluation_ids: tuple[str, ...]
    ambiguity_reasons: tuple[str, ...]


class HistoricalEvaluationCatalog:
    """Immutable structured evaluation facts; never a prose report directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: WalkForwardResult) -> Path:
        envelope = HistoricalEvaluationEnvelope(
            result_hash=content_hash(result),
            result=result,
        )
        target = self._root / f"{result.evaluation_id}.json"
        if target.exists():
            if self.load(result.evaluation_id) != result:
                raise ValueError("同一历史评价 ID 的内容不一致")
            return target

        return write_json_artifact(
            root=self._root, target=target, prefix=".evaluation-", payload=envelope
        )

    def load(self, evaluation_id: str) -> WalkForwardResult:
        target = self._root / f"{evaluation_id}.json"
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("result"), dict):
            raise ValueError("历史评价制品结构非法")
        if raw.get("result_hash") != content_hash(raw["result"]):
            raise ValueError("历史评价制品内容哈希不匹配")
        envelope = HistoricalEvaluationEnvelope.model_validate(raw)
        if envelope.result.evaluation_id != evaluation_id:
            raise ValueError("历史评价文件名与内容 ID 不一致")
        return envelope.result

    def summaries(self) -> tuple[HistoricalExperimentSummary, ...]:
        """从不可变结果确定性派生唯一有效版本；目录本身不维护可漂移索引。"""

        if not self._root.exists():
            return ()
        results = [self.load(path.stem) for path in sorted(self._root.glob("*.json"))]
        plan_identities: dict[
            tuple[str, str, str | None, str | None], tuple[str | None, str | None]
        ] = {}
        plan_groups: dict[
            tuple[str, str, str | None, str | None], list[WalkForwardResult]
        ] = defaultdict(list)
        for result in results:
            plan_groups[
                (
                    result.plan.plan_id,
                    result.dataset_id,
                    result.event_dataset_id,
                    result.funding_dataset_id,
                )
            ].append(result)
        for key, members in plan_groups.items():
            families = {_snapshot_value(item, "family") for item in members} - {None}
            versions = {_snapshot_value(item, "version") for item in members} - {None}
            plan_identities[key] = (
                next(iter(families)) if len(families) == 1 else None,
                next(iter(versions)) if len(versions) == 1 else None,
            )

        groups: dict[tuple[object, ...], list[WalkForwardResult]] = defaultdict(list)
        family_attempts: dict[str, int] = defaultdict(int)
        for result in results:
            family, version = plan_identities[
                (
                    result.plan.plan_id,
                    result.dataset_id,
                    result.event_dataset_id,
                    result.funding_dataset_id,
                )
            ]
            if family is not None:
                family_attempts[family] += 1
            if version is None:
                key = (
                    "plan",
                    result.dataset_id,
                    result.event_dataset_id,
                    result.funding_dataset_id,
                    result.plan.plan_id,
                )
            else:
                plan_policy = result.plan.model_dump(mode="json", exclude={"plan_id"})
                key = (
                    "strategy",
                    result.dataset_id,
                    result.event_dataset_id,
                    result.funding_dataset_id,
                    version,
                    content_hash(plan_policy),
                )
            groups[key].append(result)
        summaries = [
            _summarize_experiment(
                identity,
                tuple(members),
                family_attempts=family_attempts,
            )
            for identity, members in groups.items()
        ]
        return tuple(sorted(summaries, key=lambda item: item.experiment_id))


class BlindEvaluationEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: BlindEvaluationResult


class BlindEvaluationCatalog:
    """Separate immutable catalog for the single reveal of reserved labels."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: BlindEvaluationResult) -> Path:
        envelope = BlindEvaluationEnvelope(
            result_hash=content_hash(result),
            result=result,
        )
        target = self._root / f"{result.result_id}.json"
        if target.exists():
            if self.load(result.result_id) != result:
                raise ValueError("同一盲测结果 ID 的内容不一致")
            return target
        return write_json_artifact(
            root=self._root, target=target, prefix=".blind-evaluation-", payload=envelope
        )

    def load(self, result_id: str) -> BlindEvaluationResult:
        target = self._root / f"{result_id}.json"
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("result"), dict):
            raise ValueError("盲测结果制品结构非法")
        if raw.get("result_hash") != content_hash(raw["result"]):
            raise ValueError("盲测结果制品内容哈希不匹配")
        envelope = BlindEvaluationEnvelope.model_validate(raw)
        if envelope.result.result_id != result_id:
            raise ValueError("盲测结果文件名与内容 ID 不一致")
        return envelope.result


_MODEL_VERSION = re.compile(r"^investment-manager-bar-backtest-v([1-9][0-9]*)$")
_PRE_RENAME_MODEL_PREFIX_PARTS = ("quant", "core")


def _model_version_rank(model_version: str) -> int | None:
    match = _MODEL_VERSION.fullmatch(model_version)
    if match is not None:
        return int(match.group(1))

    # A product rename did not change engine semantics.  Construct the retired
    # prefix only at this compatibility boundary, so no new runtime identity or
    # artifact can emit it accidentally.
    retired_prefix = "-".join(_PRE_RENAME_MODEL_PREFIX_PARTS)
    legacy = re.fullmatch(
        rf"{re.escape(retired_prefix)}-bar-backtest-v([1-9][0-9]*)",
        model_version,
    )
    return int(legacy.group(1)) if legacy is not None else None


def _summarize_experiment(
    identity: tuple[object, ...],
    results: tuple[WalkForwardResult, ...],
    *,
    family_attempts: dict[str, int],
) -> HistoricalExperimentSummary:
    dataset_ids = {item.dataset_id for item in results}
    if len(dataset_ids) != 1:
        raise ValueError("同一历史实验包含多个数据集")
    dataset_id = next(iter(dataset_ids))
    event_dataset_ids = {item.event_dataset_id for item in results}
    if len(event_dataset_ids) != 1:
        raise ValueError("同一历史实验包含多个事件数据集")
    event_dataset_id = next(iter(event_dataset_ids))
    funding_dataset_ids = {item.funding_dataset_id for item in results}
    if len(funding_dataset_ids) != 1:
        raise ValueError("同一历史实验包含多个资金费率数据集")
    funding_dataset_id = next(iter(funding_dataset_ids))
    ranked: list[tuple[int, str, WalkForwardResult]] = []
    invalid_model_version = False
    families: set[str] = set()
    strategy_versions: set[str] = set()
    for result in results:
        model_versions = {fold.run.backtest_model_version for fold in result.folds}
        if len(model_versions) != 1:
            invalid_model_version = True
            rank = -1
            model_version = ""
        else:
            model_version = next(iter(model_versions))
            parsed_rank = _model_version_rank(model_version)
            if parsed_rank is None:
                invalid_model_version = True
                rank = -1
            else:
                rank = parsed_rank
        ranked.append((rank, model_version, result))
        snapshot = result.strategy_spec_snapshot or {}
        family = snapshot.get("family")
        version = snapshot.get("version")
        if isinstance(family, str) and family:
            families.add(family)
        if isinstance(version, str) and version:
            strategy_versions.add(version)

    ranked.sort(key=lambda item: (item[0], item[2].evaluation_id))
    reasons: list[str] = []
    if invalid_model_version:
        reasons.append("INVALID_BACKTEST_MODEL_VERSION")
    if len(families) > 1 or len(strategy_versions) > 1:
        reasons.append("STRATEGY_IDENTITY_CONFLICT")
    highest_rank = ranked[-1][0]
    highest = [item for item in ranked if item[0] == highest_rank]
    if len(highest) != 1:
        reasons.append("DUPLICATE_TOP_SEMANTICS")
    canonical = highest[0] if not reasons else None
    canonical_result = canonical[2] if canonical else None
    canonical_id = canonical_result.evaluation_id if canonical_result else None
    evaluation_ids = tuple(item[2].evaluation_id for item in ranked)
    return HistoricalExperimentSummary(
        experiment_id=stable_id("historical_experiment", identity),
        plan_ids=tuple(sorted({item.plan.plan_id for item in results})),
        dataset_id=dataset_id,
        event_dataset_id=event_dataset_id,
        funding_dataset_id=funding_dataset_id,
        strategy_family=next(iter(families)) if len(families) == 1 else None,
        strategy_versions=tuple(sorted(strategy_versions)),
        strategy_family_attempt_count=(
            family_attempts[next(iter(families))] if len(families) == 1 else None
        ),
        attempt_count=len(results),
        evaluation_ids=evaluation_ids,
        canonical_evaluation_id=canonical_id,
        canonical_backtest_model_version=canonical[1] if canonical else None,
        canonical_completed=canonical_result.completed if canonical_result else None,
        canonical_passed=canonical_result.passed if canonical_result else None,
        superseded_evaluation_ids=(
            tuple(item for item in evaluation_ids if item != canonical_id)
            if canonical_id
            else ()
        ),
        ambiguity_reasons=tuple(reasons),
    )


def _snapshot_value(result: WalkForwardResult, key: str) -> str | None:
    value = (result.strategy_spec_snapshot or {}).get(key)
    return value if isinstance(value, str) and value else None
