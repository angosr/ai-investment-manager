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
    # d1a4c7e9b205 added revision with a zero server default so existing facts
    # remained readable. Before making the field unique, preserve every old
    # snapshot and assign a deterministic causal order per portfolio. Keep the
    # JSON fact projection aligned with the indexed column because repositories
    # reconstruct the model from payload.
    snapshots = sa.table(
        "portfolio_account_snapshots",
        sa.column("snapshot_id", sa.String),
        sa.column("portfolio_id", sa.String),
        sa.column("as_of", sa.DateTime(timezone=True)),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("revision", sa.Integer),
        sa.column("payload", sa.JSON),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            snapshots.c.snapshot_id,
            snapshots.c.portfolio_id,
            snapshots.c.payload,
        ).order_by(
            snapshots.c.portfolio_id,
            snapshots.c.as_of,
            snapshots.c.observed_at,
            snapshots.c.snapshot_id,
        )
    ).mappings()
    next_revision: dict[str, int] = {}
    for row in rows:
        portfolio_id = row["portfolio_id"]
        revision = next_revision.get(portfolio_id, 0)
        payload = dict(row["payload"])
        payload["revision"] = revision
        connection.execute(
            sa.update(snapshots)
            .where(snapshots.c.snapshot_id == row["snapshot_id"])
            .values(revision=revision, payload=payload)
        )
        next_revision[portfolio_id] = revision + 1
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
