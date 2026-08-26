from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.settings import AppConfig


class EvaluationStage(StrEnum):
    STATIC = "STATIC"
    FIXED_REGRESSION = "FIXED_REGRESSION"
    WALK_FORWARD = "WALK_FORWARD"
    BLIND = "BLIND"
    FORWARD = "FORWARD"
    SHADOW = "SHADOW"
    CANARY = "CANARY"


class SystemConstitution(FrozenModel):
    version: str
    objective: str
    immutable_rules: tuple[str, ...] = Field(min_length=1)
    human_reserved_powers: tuple[str, ...] = Field(min_length=1)
    agent_forbidden_changes: tuple[str, ...] = Field(min_length=1)


class RegressionCase(FrozenModel):
    id: str
    fixture: str | None = None
    test: str | None = None
    invariants: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_has_exactly_one_target(self):
        if (self.fixture is None) == (self.test is None):
            raise ValueError("回归用例必须且只能指定 fixture 或 test")
        return self


class RegressionSuite(FrozenModel):
    version: str
    immutable: bool
    cases: tuple[RegressionCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self):
        ids = [item.id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("固定回归集 case id 必须唯一")
        if not self.immutable:
            raise ValueError("生产回归集必须标记 immutable")
        return self


class ReleaseArtifact(FrozenModel):
    artifact_id: str
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def path_must_be_repository_relative(self):
        path = Path(self.relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("ReleaseArtifact 必须使用仓库内相对路径")
        return self


class ReleaseManifest(FrozenModel):
    manifest_id: str
    created_at: datetime
    status: str
    code_version: str
    configuration_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    component_versions: tuple[tuple[str, str], ...]
    artifacts: tuple[ReleaseArtifact, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    constitution_version: str
    parent_manifest_id: str | None = None
    complexity_score: int = Field(default=0, ge=0)

    _utc_created_at = field_validator("created_at")(require_utc)

    @model_validator(mode="after")
    def artifacts_are_unique_and_sorted(self):
        ids = tuple(item.artifact_id for item in self.artifacts)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("ReleaseManifest 制品必须按唯一 ID 排序")
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("ReleaseManifest 制品路径不得重复")
        return self


class FailedExperiment(FrozenModel):
    experiment_id: str
    hypothesis_fingerprint: str
    evidence_ids: tuple[str, ...]
    rejected_at: datetime
    reason_codes: tuple[str, ...]

    _utc_rejected_at = field_validator("rejected_at")(require_utc)


def evaluation_plan_invalidation_id(plan_id: str) -> str:
    return stable_id("invalidated_evaluation_plan", plan_id)


def build_evaluation_plan_invalidation(
    *,
    plan_id: str,
    invalidated_at: datetime,
    reason_codes: tuple[str, ...],
    evidence_ids: tuple[str, ...],
) -> FailedExperiment:
    if not reason_codes or not evidence_ids:
        raise ValueError("EvaluationPlan 失效必须包含原因码和证据")
    return FailedExperiment(
        experiment_id=evaluation_plan_invalidation_id(plan_id),
        hypothesis_fingerprint=content_hash({"invalidated_evaluation_plan": plan_id}),
        evidence_ids=(f"evaluation_plan:{plan_id}", *evidence_ids),
        rejected_at=require_utc(invalidated_at),
        reason_codes=("EVALUATION_PLAN_INVALIDATED", *reason_codes),
    )


class EvaluationPlan(FrozenModel):
    plan_id: str
    registered_at: datetime
    base_manifest_id: str
    primary_metric: str
    minimum_sample_size: int = Field(gt=0)
    hard_guardrails: tuple[str, ...] = Field(min_length=1)
    required_stages: tuple[EvaluationStage, ...] = Field(min_length=1)
    fixed_regression_suite_version: str
    candidate_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_spec_snapshot: dict[str, object] | None = None
    blind_query_budget: int = Field(default=0, ge=0)

    _utc_registered_at = field_validator("registered_at")(require_utc)

    @field_validator("required_stages")
    @classmethod
    def stages_are_unique_and_ordered(
        cls, stages: tuple[EvaluationStage, ...]
    ) -> tuple[EvaluationStage, ...]:
        canonical = tuple(EvaluationStage)
        positions = [canonical.index(item) for item in stages]
        if len(set(stages)) != len(stages) or positions != sorted(positions):
            raise ValueError("评估阶段必须唯一并按固定顺序登记")
        return stages

    @model_validator(mode="after")
    def blind_stage_requires_query_budget(self):
        if EvaluationStage.BLIND in self.required_stages and self.blind_query_budget != 1:
            raise ValueError("BLIND 阶段必须预登记恰好一次查询预算")
        if self.candidate_spec_snapshot is not None and (
            self.candidate_spec_hash is None
            or content_hash(self.candidate_spec_snapshot) != self.candidate_spec_hash
        ):
            raise ValueError("EvaluationPlan 候选规格快照与哈希不一致")
        return self


class BlindEvaluationClaim(FrozenModel):
    """Single durable claim for revealing one plan's reserved blind window."""

    query_id: str
    blind_scope_id: str
    blind_symbol: str = Field(pattern=r"^[A-Z0-9]{2,32}$")
    blind_start: datetime
    blind_end: datetime
    plan_id: str
    source_evaluation_id: str
    claimed_at: datetime
    completed_at: datetime | None = None
    result_id: str | None = None
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _utc_claimed_at = field_validator("claimed_at")(require_utc)
    _utc_completed_at = field_validator("completed_at")(optional_utc)
    _utc_blind_start = field_validator("blind_start")(require_utc)
    _utc_blind_end = field_validator("blind_end")(require_utc)

    @model_validator(mode="after")
    def completion_fields_are_atomic(self):
        if self.blind_start >= self.blind_end:
            raise ValueError("盲测时间窗起点必须早于终点")
        expected_scope_id = stable_id(
            "blind_evaluation_scope",
            self.blind_symbol,
            self.blind_start,
            self.blind_end,
        )
        if self.blind_scope_id != expected_scope_id:
            raise ValueError("盲测时间窗身份与边界不一致")
        completion = (self.completed_at, self.result_id, self.result_hash)
        if any(item is not None for item in completion) and not all(
            item is not None for item in completion
        ):
            raise ValueError("盲测认领的完成字段必须同时存在")
        if self.completed_at is not None and self.completed_at < self.claimed_at:
            raise ValueError("盲测完成时间不能早于认领时间")
        return self


def load_constitution(path: str | Path) -> SystemConstitution:
    with Path(path).open("r", encoding="utf-8") as handle:
        return SystemConstitution.model_validate(yaml.safe_load(handle))


def load_regression_suite(path: str | Path) -> RegressionSuite:
    with Path(path).open("r", encoding="utf-8") as handle:
        return RegressionSuite.model_validate(yaml.safe_load(handle))


def load_release_manifest(path: str | Path) -> ReleaseManifest:
    with Path(path).open("r", encoding="utf-8") as handle:
        return ReleaseManifest.model_validate(yaml.safe_load(handle))


_CONFIG_COMPONENT_NAMES = (
    "feature",
    "decision_state",
    "capital",
    "outcome_evaluation",
    "trigger",
    "temporal",
    "market_data",
    "shadow",
    "information",
    "pipeline",
    "codex_runtime",
    "assessment",
    "codex_accounts",
    "binance_testnet",
    "governance",
)


def validate_manifest_component_versions(
    manifest: ReleaseManifest,
    config: AppConfig,
) -> None:
    """Compare stable behavior versions without reinterpreting an old config hash."""

    declared = dict(manifest.component_versions)
    current = {name: getattr(config, name).version for name in _CONFIG_COMPONENT_NAMES}
    if declared != current:
        raise ValueError("ReleaseManifest 与当前类型化行为配置版本不一致")


def validate_manifest_against_config(
    manifest: ReleaseManifest,
    config: AppConfig,
    *,
    require_configuration_hash: bool = False,
) -> None:
    validate_manifest_component_versions(manifest, config)
    if manifest.configuration_hash is None:
        if require_configuration_hash:
            raise ValueError("运行 ReleaseManifest 缺少完整配置哈希")
        return
    if manifest.configuration_hash != content_hash(config):
        raise ValueError("ReleaseManifest 与当前完整配置内容不一致")


def validate_manifest_code_version(
    manifest: ReleaseManifest,
    *,
    repository_root: Path | None = None,
) -> Path:
    """Fail closed unless runtime imports come from the exact clean release commit."""

    root = (repository_root or _source_repository_root()).resolve()
    current_clean_code_version(
        repository_root=root,
        expected_version=manifest.code_version,
    )
    return root


def validate_runtime_release_checkout(repository_root: Path) -> None:
    """Runtime must import from a detached release checkout, never the dev tree."""

    completed = subprocess.run(
        ("git", "-C", str(repository_root), "symbolic-ref", "-q", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode == 0:
        raise ValueError("运行服务必须从 detached 的冻结 Release checkout 启动")
    if completed.returncode not in {1}:
        raise ValueError("无法确认 Release checkout 是否冻结")


def validate_manifest_artifacts(
    manifest: ReleaseManifest,
    *,
    repository_root: Path,
    required_ids: tuple[str, ...] = (),
) -> None:
    by_id = {item.artifact_id: item for item in manifest.artifacts}
    missing = set(required_ids) - set(by_id)
    if missing:
        raise ValueError(f"ReleaseManifest 缺少必需制品：{', '.join(sorted(missing))}")
    for artifact in manifest.artifacts:
        path = (repository_root / artifact.relative_path).resolve()
        try:
            path.relative_to(repository_root)
        except ValueError as exc:
            raise ValueError("ReleaseArtifact 解析到仓库目录之外") from exc
        observed = _artifact_sha256(path)
        if observed != artifact.sha256:
            raise ValueError(
                f"ReleaseArtifact 内容不一致：{artifact.artifact_id}"
            )


def resolve_manifest_artifact(
    manifest: ReleaseManifest,
    artifact_id: str,
    *,
    repository_root: Path | None = None,
) -> Path:
    root = (repository_root or _source_repository_root()).resolve()
    artifact = next(
        (item for item in manifest.artifacts if item.artifact_id == artifact_id),
        None,
    )
    if artifact is None:
        raise ValueError(f"ReleaseManifest 缺少制品：{artifact_id}")
    path = (root / artifact.relative_path).resolve()
    validate_manifest_artifacts(
        manifest.model_copy(update={"artifacts": (artifact,)}),
        repository_root=root,
        required_ids=(artifact_id,),
    )
    return path


def _artifact_sha256(path: Path) -> str:
    if path.is_symlink() or not path.exists():
        raise ValueError(f"ReleaseArtifact 不存在或为符号链接：{path}")
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"ReleaseArtifact 类型不受支持：{path}")
    files = tuple(
        sorted(
            item
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )
    )
    if any(item.is_symlink() for item in path.rglob("*")):
        raise ValueError(f"ReleaseArtifact 目录不得包含符号链接：{path}")
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        payload = item.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def current_clean_code_version(
    *,
    repository_root: Path | None = None,
    expected_version: str | None = None,
) -> str:
    """Return the exact commit only when all runtime-bearing paths are clean."""

    root = (repository_root or _source_repository_root()).resolve()
    head = _git_output(root, "rev-parse", "HEAD")
    if expected_version is not None and head != expected_version:
        raise ValueError(
            "ReleaseManifest 代码版本与实际运行源码不一致："
            f"expected={expected_version}, observed={head}"
        )
    dirty = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "src",
        "config",
        "migrations",
        "pyproject.toml",
        "web/src",
        "web/package.json",
        "web/package-lock.json",
    )
    if dirty:
        raise ValueError("ReleaseManifest 对应的运行源码存在未提交变更")
    return head


def committed_file_revision(
    path: Path,
    *,
    repository_root: Path | None = None,
) -> tuple[str, datetime]:
    """Return the commit that froze the exact current bytes of one tracked file."""

    root = (repository_root or _source_repository_root()).resolve()
    target = path.resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("预登记文件必须位于治理源码仓库内") from exc
    dirty = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        relative,
    )
    if dirty:
        raise ValueError("预登记文件尚未提交或存在未提交变更")
    commit = _git_output(root, "log", "-1", "--format=%H", "--", relative)
    if not commit:
        raise ValueError("预登记文件没有可验证的提交历史")
    committed_blob = _git_output(root, "rev-parse", f"{commit}:{relative}")
    current_blob = _git_output(root, "hash-object", relative)
    if committed_blob != current_blob:
        raise ValueError("预登记文件内容与登记提交不一致")
    committed_at = datetime.fromisoformat(
        _git_output(root, "show", "-s", "--format=%cI", commit)
    )
    return commit, require_utc(committed_at)


def _source_repository_root() -> Path:
    source = Path(__file__).resolve()
    for candidate in source.parents:
        if (candidate / ".git").exists():
            return candidate
    raise ValueError("无法定位运行源码的 Git 仓库；拒绝接受未验证代码版本")


def _git_output(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法验证 ReleaseManifest 代码版本") from exc
    if completed.returncode != 0:
        raise ValueError("无法验证 ReleaseManifest 代码版本")
    return completed.stdout.strip()
