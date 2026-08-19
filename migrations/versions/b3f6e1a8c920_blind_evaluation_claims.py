"""one-time blind evaluation claims

Revision ID: b3f6e1a8c920
Revises: c4e91b7d2a60
Create Date: 2026-08-19 19:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f6e1a8c920"
down_revision: str | Sequence[str] | None = "c4e91b7d2a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blind_evaluation_claims",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("query_id", sa.String(length=128), nullable=False),
        sa.Column("source_evaluation_id", sa.String(length=128), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_id", sa.String(length=128), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["evaluation_plans.plan_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("query_id"),
    )


def downgrade() -> None:
    op.drop_table("blind_evaluation_claims")
