"""add content-addressed raw source payloads

Revision ID: b6e2d8a4c913
Revises: a3c9e5f7b204
Create Date: 2026-08-20 12:51:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2d8a4c913"
down_revision: str | Sequence[str] | None = "a3c9e5f7b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_source_payloads",
        sa.Column("payload_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("payload_id"),
    )
    op.create_index(
        "ix_raw_source_payloads_source_observed",
        "raw_source_payloads",
        ["source_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_raw_source_payloads_source_observed",
        table_name="raw_source_payloads",
    )
    op.drop_table("raw_source_payloads")
