"""classify source document observations by information expansion

Revision ID: e9a5b2c7d130
Revises: d8f4a1c9e620
Create Date: 2026-08-29 20:00:00
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9a5b2c7d130"
down_revision: str | Sequence[str] | None = "d8f4a1c9e620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "normalized_events",
        sa.Column(
            "expands_document_information",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    connection = op.get_bind()
    events = sa.table(
        "normalized_events",
        sa.column("evidence_id", sa.String()),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("source", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("expands_document_information", sa.Boolean()),
    )
    histories: dict[tuple[str, str], list[dict]] = {}
    rows = connection.execute(
        sa.select(
            events.c.evidence_id,
            events.c.source,
            events.c.payload,
        ).order_by(events.c.observed_at, events.c.evidence_id)
    ).mappings()
    for row in rows:
        payload = row["payload"]
        url = payload.get("url")
        expands = True
        if isinstance(url, str) and url:
            key = (row["source"], url)
            history = histories.setdefault(key, [])
            expands = _adds_information(payload, history)
            history.append(payload)
        connection.execute(
            events.update()
            .where(events.c.evidence_id == row["evidence_id"])
            .values(expands_document_information=expands)
        )


def downgrade() -> None:
    op.drop_column("normalized_events", "expands_document_information")


def _adds_information(event: dict, history: list[dict]) -> bool:
    if not history:
        return True
    known_symbols = {
        symbol
        for item in history
        for symbol in item.get("symbols", ())
        if isinstance(symbol, str)
    }
    event_symbols = {
        symbol for symbol in event.get("symbols", ()) if isinstance(symbol, str)
    }
    if not event_symbols.issubset(known_symbols):
        return True
    historical_fragments = tuple(
        fragment for item in history for fragment in _fragments(item)
    )
    return any(
        not any(_covered(fragment, historical) for historical in historical_fragments)
        for fragment in _fragments(event)
    )


def _fragments(event: dict) -> tuple[str, ...]:
    fragments: list[str] = []
    for field in ("title", "body", "decision_excerpt"):
        value = event.get(field, "")
        if not isinstance(value, str):
            continue
        normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        if normalized and normalized not in fragments:
            fragments.append(normalized)
    return tuple(fragments)


def _covered(fragment: str, historical: str) -> bool:
    start = historical.find(fragment)
    while start >= 0:
        end = start + len(fragment)
        left_boundary = (
            start == 0 or not fragment[0].isalnum() or not historical[start - 1].isalnum()
        )
        right_boundary = (
            end == len(historical)
            or not fragment[-1].isalnum()
            or not historical[end].isalnum()
        )
        if left_boundary and right_boundary:
            return True
        start = historical.find(fragment, start + 1)
    return False
