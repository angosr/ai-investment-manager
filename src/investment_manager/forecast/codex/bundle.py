from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from investment_manager.kernel.identity import content_hash


def validated_behavior_hash(value: object) -> str | None:
    if value is None:
        return None
    if not (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        raise ValueError("analysis_behavior_hash 必须是 64 位十六进制摘要")
    return value


@dataclass(frozen=True, slots=True)
class RunBundle:
    cycle_id: str
    path: Path
    bundle_hash: str
    prompt: str
    analysis_behavior_hash: str | None = None


def write_run_bundle(
    *,
    cycle_id: str,
    target: Path,
    prompt: str,
    files: dict[str, str],
    manifest: dict[str, Any],
) -> RunBundle:
    behavior_hash = validated_behavior_hash(manifest.get("analysis_behavior_hash"))
    if target.exists() and any(target.iterdir()):
        raise ValueError("运行包目录必须为空")
    target.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (target / name).write_text(value, encoding="utf-8")
    complete_manifest = {
        **manifest,
        "cycle_id": cycle_id,
        "files": {name: content_hash({"content": value}) for name, value in files.items()},
    }
    manifest_text = (
        json.dumps(
            complete_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    (target / "manifest.json").write_text(manifest_text, encoding="utf-8")
    for child in target.iterdir():
        child.chmod(0o444)
    target.chmod(0o555)
    return RunBundle(
        cycle_id=cycle_id,
        path=target,
        bundle_hash=content_hash({"manifest": complete_manifest}),
        prompt=prompt,
        analysis_behavior_hash=behavior_hash,
    )


def verify_bundle(bundle: RunBundle) -> bool:
    try:
        manifest = json.loads((bundle.path / "manifest.json").read_text(encoding="utf-8"))
        for name, expected in manifest["files"].items():
            value = (bundle.path / name).read_text(encoding="utf-8")
            if content_hash({"content": value}) != expected:
                return False
        return content_hash({"manifest": manifest}) == bundle.bundle_hash
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def load_existing_bundle(
    *,
    cycle_id: str,
    target: Path,
    expected_manifest: Mapping[str, object] | None = None,
) -> RunBundle | None:
    if not target.exists():
        return None
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if expected_manifest is not None and any(
        manifest.get(key) != value for key, value in expected_manifest.items()
    ):
        raise ValueError("已有运行包身份不匹配")
    bundle = RunBundle(
        cycle_id=cycle_id,
        path=target,
        bundle_hash=content_hash({"manifest": manifest}),
        prompt=(target / "analyst_prompt.md").read_text(encoding="utf-8").strip(),
        analysis_behavior_hash=validated_behavior_hash(manifest.get("analysis_behavior_hash")),
    )
    if not verify_bundle(bundle):
        raise ValueError("已有运行包校验失败")
    return bundle
