"""add point-in-time perpetual market facts

Revision ID: a6e4d9b2c731
Revises: d4b7e2c9a106
Create Date: 2026-08-21 02:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6e4d9b2c731"
down_revision: str | Sequence[str] | None = "d4b7e2c9a106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perpetual_market_states",
        sa.Column("state_id", sa.String(length=128), nullable=False),
        sa.Column("instrument_id", sa.String(length=128), nullable=False),
        sa.Column("exchange_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("state_id"),
    )
    op.create_index(
        "ix_perpetual_market_states_instrument_time",
        "perpetual_market_states",
        ["instrument_id", "exchange_time", "observed_at"],
    )
    op.create_table(
        "funding_settlements",
        sa.Column("settlement_id", sa.String(length=128), nullable=False),
        sa.Column("instrument_id", sa.String(length=128), nullable=False),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate_type", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("settlement_id"),
    )
    op.create_index(
        "ix_funding_settlements_instrument_time",
        "funding_settlements",
        ["instrument_id", "funding_time", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_funding_settlements_instrument_time",
        table_name="funding_settlements",
    )
    op.drop_table("funding_settlements")
    op.drop_index(
        "ix_perpetual_market_states_instrument_time",
        table_name="perpetual_market_states",
    )
    op.drop_table("perpetual_market_states")
