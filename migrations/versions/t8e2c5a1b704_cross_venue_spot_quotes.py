"""persist independent cross-venue spot quotes

Revision ID: t8e2c5a1b704
Revises: s7c4e1a9f263
Create Date: 2026-08-25 22:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t8e2c5a1b704"
down_revision: str | Sequence[str] | None = "s7c4e1a9f263"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cross_venue_spot_quotes",
        sa.Column("quote_id", sa.String(length=128), nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("exchange_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("quote_id"),
    )
    op.create_index(
        "ix_cross_venue_spot_quotes_venue_symbol_time",
        "cross_venue_spot_quotes",
        ["venue", "symbol", "exchange_time", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cross_venue_spot_quotes_venue_symbol_time",
        table_name="cross_venue_spot_quotes",
    )
    op.drop_table("cross_venue_spot_quotes")
