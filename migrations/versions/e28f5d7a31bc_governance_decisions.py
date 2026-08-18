"""governance decisions

Revision ID: e28f5d7a31bc
Revises: d17e4c8b902a
Create Date: 2026-08-18 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e28f5d7a31bc"
down_revision: str | Sequence[str] | None = "d17e4c8b902a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governance_decisions",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["governance_snapshots.snapshot_id"]),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("snapshot_id"),
    )


def downgrade() -> None:
    op.drop_table("governance_decisions")
