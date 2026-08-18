"""market data projection

Revision ID: 2d4d11cb93fa
Revises: 7f4b9f48bb32
Create Date: 2026-08-18 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d4d11cb93fa"
down_revision: str | Sequence[str] | None = "7f4b9f48bb32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_bars",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "interval", "open_time"),
    )
    op.create_index(
        "ix_market_bars_symbol_interval_open",
        "market_bars",
        ["symbol", "interval", "open_time"],
        unique=False,
    )
    op.create_table(
        "market_quotes",
        sa.Column("quote_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("quote_id"),
    )
    op.create_index(
        "ix_market_quotes_symbol_observed",
        "market_quotes",
        ["symbol", "observed_at"],
        unique=False,
    )
    op.create_table(
        "market_trades",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("aggregate_trade_id", sa.Integer(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "aggregate_trade_id"),
    )
    op.create_index(
        "ix_market_trades_symbol_event",
        "market_trades",
        ["symbol", "event_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_market_trades_symbol_event", table_name="market_trades")
    op.drop_table("market_trades")
    op.drop_index("ix_market_quotes_symbol_observed", table_name="market_quotes")
    op.drop_table("market_quotes")
    op.drop_index("ix_market_bars_symbol_interval_open", table_name="market_bars")
    op.drop_table("market_bars")
