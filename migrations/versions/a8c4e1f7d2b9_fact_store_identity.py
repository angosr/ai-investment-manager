"""add physical fact store role identity

Revision ID: a8c4e1f7d2b9
Revises: f4b7c2d8e901
Create Date: 2026-08-22 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c4e1f7d2b9"
down_revision: str | Sequence[str] | None = "f4b7c2d8e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fact_store_identity",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("singleton_key = 'PRIMARY'", name="ck_fact_store_singleton"),
        sa.CheckConstraint("role IN ('CAPITAL', 'CONTEXT')", name="ck_fact_store_role"),
        sa.PrimaryKeyConstraint("singleton_key"),
        sa.UniqueConstraint("store_id"),
    )


def downgrade() -> None:
    op.drop_table("fact_store_identity")
