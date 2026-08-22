"""Stable opaque cursors for immutable dashboard timelines."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

_CURSOR_VERSION = 1
_MAX_IDENTITY_LENGTH = 512


class InvalidPageCursor(ValueError):
    """The caller supplied a cursor that was not issued by this API contract."""


@dataclass(frozen=True, slots=True)
class PageCursor:
    at: datetime
    identity: str

    def __post_init__(self) -> None:
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise InvalidPageCursor("分页游标时间必须包含时区")
        if not self.identity or len(self.identity) > _MAX_IDENTITY_LENGTH:
            raise InvalidPageCursor("分页游标记录身份无效")


@dataclass(frozen=True, slots=True)
class PageSlice[T]:
    items: tuple[T, ...]
    next_cursor: str | None


def encode_page_cursor(cursor: PageCursor) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "at": cursor.at.astimezone(UTC).isoformat(),
            "id": cursor.identity,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_page_cursor(raw: str) -> PageCursor:
    try:
        padding = "=" * (-len(raw) % 4)
        payload = json.loads(base64.b64decode(raw + padding, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {"v", "at", "id"}:
            raise ValueError
        if payload["v"] != _CURSOR_VERSION or not isinstance(payload["id"], str):
            raise ValueError
        at = datetime.fromisoformat(payload["at"])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidPageCursor("分页游标无效或版本不受支持") from exc
    return PageCursor(at=at, identity=payload["id"])


def older_than(
    at_column: ColumnElement,
    identity_column: ColumnElement,
    cursor: PageCursor,
) -> ColumnElement[bool]:
    """SQL predicate matching descending ``(at, identity)`` keyset order."""

    return or_(
        at_column < cursor.at,
        and_(at_column == cursor.at, identity_column < cursor.identity),
    )


def page_slice[T](
    values: Sequence[T],
    *,
    limit: int,
    cursor_for: Callable[[T], PageCursor],
) -> PageSlice[T]:
    if limit < 1:
        raise ValueError("分页大小必须为正数")
    visible = tuple(values[:limit])
    next_cursor = (
        encode_page_cursor(cursor_for(visible[-1]))
        if len(values) > limit and visible
        else None
    )
    return PageSlice(items=visible, next_cursor=next_cursor)
