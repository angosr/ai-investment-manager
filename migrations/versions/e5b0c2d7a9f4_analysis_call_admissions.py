"""global analysis call admissions

Revision ID: e5b0c2d7a9f4
Revises: d4a9f1c6b8e2
Create Date: 2026-08-19 04:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b0c2d7a9f4"
down_revision: str | Sequence[str] | None = "d4a9f1c6b8e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    admissions = op.create_table(
        "analysis_call_admissions",
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    batches = sa.table(
        "analysis_trigger_batches",
        sa.column("batch_id", sa.String(length=128)),
        sa.column("pipeline_id", sa.String(length=128)),
        sa.column("symbol", sa.String(length=32)),
        sa.column("analysis_submitted_at", sa.DateTime(timezone=True)),
        sa.column("payload", sa.JSON()),
    )
    op.execute(
        admissions.insert().from_select(
            ["batch_id", "pipeline_id", "symbol", "admitted_at", "payload"],
            sa.select(
                batches.c.batch_id,
                batches.c.pipeline_id,
                batches.c.symbol,
                batches.c.analysis_submitted_at,
                batches.c.payload,
            ),
        )
    )
    op.create_index(
        "ix_analysis_call_admissions_admitted_at",
        "analysis_call_admissions",
        ["admitted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_call_admissions_admitted_at",
        table_name="analysis_call_admissions",
    )
    op.drop_table("analysis_call_admissions")
