"""add content-addressed state evidence snapshots

Revision ID: c7f4a1d9e205
Revises: b6e2d8a4c913
Create Date: 2026-08-20 13:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f4a1d9e205"
down_revision: str | Sequence[str] | None = "b6e2d8a4c913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "state_evidence_snapshots",
        sa.Column("evidence_ref", sa.String(length=64), nullable=False),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("evidence_ref"),
    )


def downgrade() -> None:
    op.drop_table("state_evidence_snapshots")
