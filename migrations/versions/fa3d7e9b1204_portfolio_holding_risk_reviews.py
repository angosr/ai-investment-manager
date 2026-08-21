"""Add explicit intra-month holding risk review facts.

Revision ID: fa3d7e9b1204
Revises: f9a2c4e6b801
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa3d7e9b1204"
down_revision: str | None = "f9a2c4e6b801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio_account_snapshots") as batch_op:
        batch_op.create_unique_constraint(
            "uq_portfolio_account_revision",
            ["portfolio_id", "revision"],
        )
    op.create_table(
        "portfolio_holding_risk_reviews",
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("account_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["portfolio_account_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint(
            "account_snapshot_id",
            "policy_version",
            name="uq_holding_risk_account_policy",
        ),
        sa.UniqueConstraint("review_hash"),
    )
    op.create_index(
        "ix_holding_risk_portfolio_time",
        "portfolio_holding_risk_reviews",
        ["portfolio_id", "reviewed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_holding_risk_portfolio_time",
        table_name="portfolio_holding_risk_reviews",
    )
    op.drop_table("portfolio_holding_risk_reviews")
    with op.batch_alter_table("portfolio_account_snapshots") as batch_op:
        batch_op.drop_constraint(
            "uq_portfolio_account_revision",
            type_="unique",
        )
