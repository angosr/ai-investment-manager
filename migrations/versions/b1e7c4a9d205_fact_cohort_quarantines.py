"""add append-only wrong-store cohort quarantines

Revision ID: b1e7c4a9d205
Revises: a8c4e1f7d2b9
Create Date: 2026-08-22 13:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1e7c4a9d205"
down_revision: str | Sequence[str] | None = "a8c4e1f7d2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fact_cohort_quarantines",
        sa.Column("quarantine_id", sa.String(length=128), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_id", sa.String(length=128), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "reason_code = 'WRONG_FACT_STORE'",
            name="ck_fact_cohort_quarantine_reason",
        ),
        sa.PrimaryKeyConstraint("quarantine_id"),
    )


def downgrade() -> None:
    op.drop_table("fact_cohort_quarantines")
