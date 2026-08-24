"""persist forecast producer behavior activation

Revision ID: j7e1a4c9f326
Revises: i6d0f3b8e215
Create Date: 2026-08-24 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "j7e1a4c9f326"
down_revision: str | Sequence[str] | None = "i6d0f3b8e215"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "forecast_producer_bindings",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    connection = op.get_bind()
    bindings = sa.table(
        "forecast_producer_bindings",
        sa.column("binding_id", sa.String()),
        sa.column("activated_at", sa.DateTime(timezone=True)),
    )
    obligations = sa.table(
        "forecast_slot_obligations",
        sa.column("binding_id", sa.String()),
        sa.column("slot_id", sa.String()),
    )
    slots = sa.table(
        "forecast_decision_slots",
        sa.column("slot_id", sa.String()),
        sa.column("slot_as_of", sa.DateTime(timezone=True)),
    )
    earliest_slot = (
        sa.select(sa.func.min(slots.c.slot_as_of))
        .select_from(
            obligations.join(slots, obligations.c.slot_id == slots.c.slot_id)
        )
        .where(obligations.c.binding_id == bindings.c.binding_id)
        .scalar_subquery()
    )
    connection.execute(
        bindings.update().values(
            activated_at=sa.func.coalesce(earliest_slot, sa.func.current_timestamp())
        )
    )
    with op.batch_alter_table("forecast_producer_bindings") as batch:
        batch.alter_column(
            "activated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    raise RuntimeError(
        "Forecast producer activation is an immutable cohort boundary and requires restore"
    )
