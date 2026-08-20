from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from quant_core.persistence import create_schema, raw_source_payloads
from quant_core.source_payload import build_raw_source_payload
from quant_core.source_payload_sql import SqlRawSourcePayloadStore

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _payload(content: bytes, *, observed_at: datetime = OBSERVED_AT):
    return build_raw_source_payload(
        source_id="federal-reserve",
        source_url="https://www.federalreserve.gov/source",
        media_type="text/plain",
        observed_at=observed_at,
        content=content,
    )


def test_raw_payload_store_is_content_addressed_and_keeps_first_seen() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlRawSourcePayloadStore(engine)
    content = b"official source document"
    first = _payload(content)
    repeated = _payload(content, observed_at=OBSERVED_AT + timedelta(minutes=1))

    assert store.put(first, content) == first
    assert store.put(repeated, content) == first
    assert store.get(first.payload_id) == (first, content)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_source_payloads)) == 1


def test_raw_payload_store_rejects_tampered_bytes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlRawSourcePayloadStore(engine)

    with pytest.raises(ValueError, match=r"byte_count|content_hash"):
        store.put(_payload(b"expected"), b"tampered")
