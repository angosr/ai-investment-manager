"""bind forecast decision slots to producer behavior obligations

Revision ID: h5c9e2a7d104
Revises: g4b8d1e6f302
Create Date: 2026-08-24 08:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h5c9e2a7d104"
down_revision: str | Sequence[str] | None = "g4b8d1e6f302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_slot_obligations",
        sa.Column("obligation_id", sa.String(length=128), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("binding_id", sa.String(length=128), nullable=False),
        sa.Column("producer_kind", sa.String(length=32), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("producer_behavior_id", sa.String(length=128), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["forecast_producer_bindings.binding_id"],
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["forecast_contracts.contract_id"]),
        sa.ForeignKeyConstraint(["slot_id"], ["forecast_decision_slots.slot_id"]),
        sa.PrimaryKeyConstraint("obligation_id"),
        sa.UniqueConstraint(
            "slot_id",
            "producer_behavior_id",
            name="uq_forecast_slot_obligation_behavior",
        ),
    )
    op.create_index(
        "ix_forecast_slot_obligations_behavior_time",
        "forecast_slot_obligations",
        ["producer_behavior_id", "assigned_at"],
    )

    # Existing slots deliberately remain unassigned. Their producer obligation
    # cannot be reconstructed from successful results without outcome leakage.


def downgrade() -> None:
    raise RuntimeError(
        "Forecast slot obligations are immutable cohort facts and require an "
        "explicit audited restore migration"
    )
