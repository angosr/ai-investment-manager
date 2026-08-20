"""Point-in-time normalization shared by every investment domain."""

from __future__ import annotations

from datetime import UTC, datetime


def require_utc(value: datetime) -> datetime:
    """Require an aware timestamp and normalize it to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)


def optional_utc(value: datetime | None) -> datetime | None:
    """Normalize an optional aware timestamp to UTC."""
    return require_utc(value) if value is not None else None
