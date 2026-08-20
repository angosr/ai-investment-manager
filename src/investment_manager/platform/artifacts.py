from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from investment_manager.kernel.identity import canonical_json


def write_json_artifact(
    *,
    root: Path,
    target: Path,
    prefix: str,
    payload: BaseModel | dict[str, Any] | list[Any] | tuple[Any, ...],
) -> Path:
    """Atomically write canonical JSON without changing content identity."""

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
