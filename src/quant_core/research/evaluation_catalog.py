from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pydantic import Field

from quant_core.domain import FrozenModel
from quant_core.ids import canonical_json, content_hash
from quant_core.research.walk_forward import WalkForwardResult


class HistoricalEvaluationEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: WalkForwardResult


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

        self._root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".evaluation-",
            suffix=".json",
            dir=self._root,
            delete=False,
        ) as temporary:
            temporary.write(canonical_json(envelope))
            temporary.flush()
            temporary_path = Path(temporary.name)
        try:
            temporary_path.replace(target)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return target

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
