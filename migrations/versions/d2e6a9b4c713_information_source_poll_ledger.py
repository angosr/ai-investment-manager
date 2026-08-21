"""add point-in-time information source poll ledger

Revision ID: d2e6a9b4c713
Revises: c8f1a4d7e209
Create Date: 2026-08-21 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2e6a9b4c713"
down_revision: str | Sequence[str] | None = "c8f1a4d7e209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_poll_records",
        sa.Column("poll_id", sa.String(length=128), nullable=False),
        sa.Column("source_stream_id", sa.String(length=128), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("poll_id"),
    )
    op.create_index(
        "ix_source_poll_records_stream_time",
        "source_poll_records",
        ["source_stream_id", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_poll_records_stream_time",
        table_name="source_poll_records",
    )
    op.drop_table("source_poll_records")
