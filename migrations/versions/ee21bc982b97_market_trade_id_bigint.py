"""market trade id bigint

Revision ID: ee21bc982b97
Revises: 2d4d11cb93fa
Create Date: 2026-08-18 11:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ee21bc982b97"
down_revision: str | Sequence[str] | None = "2d4d11cb93fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("market_trades") as batch_op:
        batch_op.alter_column(
            "aggregate_trade_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("market_trades") as batch_op:
        batch_op.alter_column(
            "aggregate_trade_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
