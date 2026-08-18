"""execution handoff

Revision ID: 5bc92ed9ec70
Revises: ee21bc982b97
Create Date: 2026-08-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5bc92ed9ec70"
down_revision: str | Sequence[str] | None = "ee21bc982b97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_requests",
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("cycle_id", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["cycle_id"], ["analysis_cycles.cycle_id"]),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.UniqueConstraint("cycle_id"),
        sa.UniqueConstraint("request_hash"),
    )
    op.create_index(
        "ix_execution_requests_status",
        "execution_requests",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_requests_status", table_name="execution_requests")
    op.drop_table("execution_requests")
