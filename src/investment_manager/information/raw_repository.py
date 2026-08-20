from __future__ import annotations

import hashlib

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.information.raw_payload import RawSourcePayload
from investment_manager.information.tables import raw_source_payloads


class SqlRawSourcePayloadStore:
    """Content-addressed raw evidence; unchanged polls reuse the first-seen blob."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, payload: RawSourcePayload, content: bytes) -> RawSourcePayload:
        if len(content) != payload.byte_count:
            raise ValueError("RawSourcePayload byte_count 与原文不一致")
        if hashlib.sha256(content).hexdigest() != payload.content_hash:
            raise ValueError("RawSourcePayload content_hash 与原文不一致")
        try:
            with self._engine.begin() as connection:
                existing_payload = connection.execute(
                    select(raw_source_payloads.c.payload).where(
                        raw_source_payloads.c.payload_id == payload.payload_id
                    )
                ).scalar_one_or_none()
                if existing_payload is not None:
                    existing = RawSourcePayload.model_validate(existing_payload)
                    _require_same_content_identity(existing, payload)
                    return existing
                connection.execute(
                    insert(raw_source_payloads).values(
                        payload_id=payload.payload_id,
                        source_id=payload.source_id,
                        source_url=payload.source_url,
                        media_type=payload.media_type,
                        observed_at=payload.observed_at,
                        content_hash=payload.content_hash,
                        byte_count=payload.byte_count,
                        content=content,
                        payload=payload.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            raced = self.get(payload.payload_id)
            if raced is None or raced[1] != content:
                raise
            _require_same_content_identity(raced[0], payload)
            return raced[0]
        return payload

    def get(self, payload_id: str) -> tuple[RawSourcePayload, bytes] | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(raw_source_payloads.c.payload, raw_source_payloads.c.content).where(
                    raw_source_payloads.c.payload_id == payload_id
                )
            ).one_or_none()
        if row is None:
            return None
        return RawSourcePayload.model_validate(row.payload), row.content


def _require_same_content_identity(
    existing: RawSourcePayload,
    candidate: RawSourcePayload,
) -> None:
    if (
        existing.source_id,
        existing.source_url,
        existing.media_type,
        existing.content_hash,
        existing.byte_count,
    ) != (
        candidate.source_id,
        candidate.source_url,
        candidate.media_type,
        candidate.content_hash,
        candidate.byte_count,
    ):
        raise ValueError("同一 RawSourcePayload payload_id 对应不同来源元数据")
