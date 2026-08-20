from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from quant_core.asset_management import SHA256_PATTERN
from quant_core.domain import FrozenModel, _require_utc
from quant_core.ids import stable_id


class RawSourcePayload(FrozenModel):
    payload_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1, max_length=2_000)
    media_type: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    content_hash: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(gt=0)

    _utc_observed_at = field_validator("observed_at")(_require_utc)

    @model_validator(mode="after")
    def identity_must_match_content_address(self):
        expected = stable_id(
            "raw_source_payload",
            self.source_id,
            self.source_url,
            self.content_hash,
        )
        if self.payload_id != expected:
            raise ValueError("RawSourcePayload payload_id 与内容地址不一致")
        return self


def build_raw_source_payload(
    *,
    source_id: str,
    source_url: str,
    media_type: str,
    observed_at: datetime,
    content: bytes,
) -> RawSourcePayload:
    if not content:
        raise ValueError("原始来源 payload 不能为空")
    digest = hashlib.sha256(content).hexdigest()
    return RawSourcePayload(
        payload_id=stable_id("raw_source_payload", source_id, source_url, digest),
        source_id=source_id,
        source_url=source_url,
        media_type=media_type,
        observed_at=observed_at,
        content_hash=digest,
        byte_count=len(content),
    )
