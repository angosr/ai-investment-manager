"""add point-in-time perpetual executable quotes

Revision ID: e8f2c6a1d409
Revises: a6e4d9b2c731
Create Date: 2026-08-21 03:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8f2c6a1d409"
down_revision: str | Sequence[str] | None = "a6e4d9b2c731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perpetual_quotes",
        sa.Column("quote_id", sa.String(length=128), nullable=False),
        sa.Column("instrument_id", sa.String(length=128), nullable=False),
        sa.Column("exchange_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("quote_id"),
    )
    op.create_index(
        "ix_perpetual_quotes_instrument_time",
        "perpetual_quotes",
        ["instrument_id", "exchange_time", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_perpetual_quotes_instrument_time",
        table_name="perpetual_quotes",
    )
    op.drop_table("perpetual_quotes")
