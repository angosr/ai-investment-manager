from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from sqlalchemy.engine import Connection


def advisory_xact_lock(
    connection: Connection,
    namespace: str,
    *logical_identity: str,
) -> None:
    """Serialize a logical aggregate on PostgreSQL for the current transaction."""

    if connection.dialect.name != "postgresql":
        return
    identity = "\0".join((namespace, *logical_identity))
    digest = hashlib.sha256(identity.encode()).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    connection.execute(select(func.pg_advisory_xact_lock(lock_key))).scalar_one()
