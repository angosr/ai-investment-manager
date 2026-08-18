"""release approval requests

Revision ID: f39a6e8c42dd
Revises: e28f5d7a31bc
Create Date: 2026-08-18 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f39a6e8c42dd"
down_revision: str | Sequence[str] | None = "e28f5d7a31bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_approval_requests",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("evaluation_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_manifest_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["evaluation_id"], ["evaluation_results.evaluation_id"]),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("evaluation_id"),
    )


def downgrade() -> None:
    op.drop_table("release_approval_requests")
