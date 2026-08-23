"""unify context and capital in one authoritative runtime fact store

Revision ID: e2f6a8c4d901
Revises: d7a9c2e4f601
Create Date: 2026-08-23 15:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f6a8c4d901"
down_revision: str | Sequence[str] | None = "d7a9c2e4f601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve the old operational boundary as history, but remove it from every
    # current read/write path.  Forecast permissions and immutable release
    # identities now provide the boundary without splitting one decision cycle.
    op.create_table(
        "historical_fact_store_identities",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("singleton_key"),
    )
    op.execute(
        sa.text("INSERT INTO historical_fact_store_identities SELECT * FROM fact_store_identity")
    )
    op.create_table(
        "historical_fact_cohort_quarantines",
        sa.Column("quarantine_id", sa.String(length=128), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_id", sa.String(length=128), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("quarantine_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO historical_fact_cohort_quarantines "
            "SELECT * FROM fact_cohort_quarantines"
        )
    )
    op.drop_table("fact_cohort_quarantines")
    op.drop_table("fact_store_identity")


def downgrade() -> None:
    raise RuntimeError(
        "Unified fact-store migration removes an obsolete runtime boundary and "
        "cannot be downgraded without an explicit audited restore migration"
    )
