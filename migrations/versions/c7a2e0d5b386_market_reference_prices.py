"""add point-in-time Spot reference prices

Revision ID: c7a2e0d5b386
Revises: b6f1d9c4a275
Create Date: 2026-08-29 01:24:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a2e0d5b386"
down_revision: str | Sequence[str] | None = "b6f1d9c4a275"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_reference_prices",
        sa.Column("reference_price_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("exchange_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("reference_price_id"),
    )
    op.create_index(
        "ix_market_reference_prices_symbol_time",
        "market_reference_prices",
        ["symbol", "exchange_time", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_reference_prices_symbol_time",
        table_name="market_reference_prices",
    )
    op.drop_table("market_reference_prices")
