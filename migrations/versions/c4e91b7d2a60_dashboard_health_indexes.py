"""dashboard control-plane health indexes

Revision ID: c4e91b7d2a60
Revises: a9e2b7c4d6f1
Create Date: 2026-08-19 16:02:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e91b7d2a60"
down_revision: str | Sequence[str] | None = "a9e2b7c4d6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_analysis_cycles_pipeline_as_of",
        "analysis_cycles",
        ["pipeline_version", "as_of"],
        unique=False,
    )
    op.create_index(
        "ix_codex_runs_cycle_status",
        "codex_runs",
        ["cycle_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_codex_runs_cycle_status", table_name="codex_runs")
    op.drop_index("ix_analysis_cycles_pipeline_as_of", table_name="analysis_cycles")
