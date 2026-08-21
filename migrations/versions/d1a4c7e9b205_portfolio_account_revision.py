"""order same-time portfolio account projections causally

Revision ID: d1a4c7e9b205
Revises: c5a8e2f7d410
Create Date: 2026-08-21 06:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1a4c7e9b205"
down_revision: str | Sequence[str] | None = "c5a8e2f7d410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portfolio_account_snapshots",
        sa.Column(
            "revision",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.drop_index(
        "ix_portfolio_account_as_of",
        table_name="portfolio_account_snapshots",
    )
    op.create_index(
        "ix_portfolio_account_as_of",
        "portfolio_account_snapshots",
        ["portfolio_id", "as_of", "revision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_account_as_of",
        table_name="portfolio_account_snapshots",
    )
    op.create_index(
        "ix_portfolio_account_as_of",
        "portfolio_account_snapshots",
        ["portfolio_id", "as_of"],
    )
    op.drop_column("portfolio_account_snapshots", "revision")
