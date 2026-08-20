from __future__ import annotations

from datetime import UTC, datetime


def database_utc(value: datetime) -> datetime:
    """Normalize a database timestamp whose driver contract is UTC.

    Some supported drivers return UTC columns without ``tzinfo``.  Only SQL
    adapter/read-model code may use this coercion; domain inputs must continue
    to use strict timezone validation.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
