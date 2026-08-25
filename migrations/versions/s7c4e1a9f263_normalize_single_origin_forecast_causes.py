"""normalize retired empty fields from single-origin Forecast slot causes

Revision ID: s7c4e1a9f263
Revises: r6a9d3e2f842
Create Date: 2026-08-25 16:12:00
"""

from collections.abc import Sequence
from copy import deepcopy

import sqlalchemy as sa
from alembic import op

revision: str = "s7c4e1a9f263"
down_revision: str | Sequence[str] | None = "r6a9d3e2f842"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _canonical_payload(payload: dict[str, object]) -> dict[str, object]:
    cause = payload.get("cause")
    if not isinstance(cause, dict):
        return payload
    if "additional_origins" not in cause and "cadence_anchor_at" not in cause:
        return payload
    if cause.get("additional_origins") not in (None, []) or cause.get(
        "cadence_anchor_at"
    ) is not None:
        raise RuntimeError(
            "发现真实 combined Forecast slot；必须先以独立审计迁移归档，"
            "不得伪装为单一来源"
        )
    canonical = deepcopy(payload)
    canonical_cause = canonical["cause"]
    assert isinstance(canonical_cause, dict)
    canonical_cause.pop("additional_origins", None)
    canonical_cause.pop("cadence_anchor_at", None)
    return canonical


def upgrade() -> None:
    slots = sa.table(
        "forecast_decision_slots",
        sa.column("slot_id", sa.String(length=128)),
        sa.column("payload", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(slots.c.slot_id, slots.c.payload)
    ).mappings().all()
    updates = tuple(
        (row["slot_id"], canonical)
        for row in rows
        if (canonical := _canonical_payload(row["payload"])) != row["payload"]
    )
    for slot_id, canonical in updates:
        connection.execute(
            sa.update(slots)
            .where(slots.c.slot_id == slot_id)
            .values(payload=canonical)
        )


def downgrade() -> None:
    raise RuntimeError(
        "空 combined 字段已从单一来源事实中规范化；不能重新注入退役字段"
    )
