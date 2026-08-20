from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python


def canonical_json(value: BaseModel | dict[str, Any] | list[Any] | tuple[Any, ...]) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: BaseModel | dict[str, Any] | list[Any] | tuple[Any, ...]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def write_json_artifact(
    *,
    root: Path,
    target: Path,
    prefix: str,
    payload: BaseModel | dict[str, Any] | list[Any] | tuple[Any, ...],
) -> Path:
    """把 payload 以 canonical_json 原子写入 target：同目录临时文件写入后 replace。

    内容序列化与产物字节完全由 canonical_json 决定，本助手只封装"临时文件 → 原子
    替换 → 失败清理"这一文件系统样板，供内容寻址制品仓库共享，不改变任何产物哈希。
    """
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=prefix,
        suffix=".json",
        dir=root,
        delete=False,
    ) as temporary:
        temporary.write(canonical_json(payload))
        temporary.flush()
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return target
