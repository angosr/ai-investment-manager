from __future__ import annotations

import hashlib
import json
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
