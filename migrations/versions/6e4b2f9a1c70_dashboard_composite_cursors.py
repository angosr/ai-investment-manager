"""Add stable keyset-pagination indexes for dashboard timelines.

Revision ID: 6e4b2f9a1c70
Revises: f4b7c2d8e901
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6e4b2f9a1c70"
down_revision: str | None = "f4b7c2d8e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_analysis_cycles_as_of", table_name="analysis_cycles")
    op.create_index(
        "ix_analysis_cycles_cursor",
        "analysis_cycles",
        ["as_of", "cycle_id"],
        unique=False,
    )
    op.drop_index("ix_normalized_events_event_time", table_name="normalized_events")
    op.create_index(
        "ix_normalized_events_cursor",
        "normalized_events",
        ["event_time", "evidence_id"],
        unique=False,
    )
    op.create_index(
        "ix_context_assessments_cursor",
        "context_assessments",
        ["available_at", "assessment_id"],
        unique=False,
    )
    op.create_index(
        "ix_analysis_trigger_events_cursor",
        "analysis_trigger_events",
        ["occurred_at", "trigger_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_trigger_events_cursor",
        table_name="analysis_trigger_events",
    )
    op.drop_index("ix_context_assessments_cursor", table_name="context_assessments")
    op.drop_index("ix_normalized_events_cursor", table_name="normalized_events")
    op.create_index(
        "ix_normalized_events_event_time",
        "normalized_events",
        ["event_time"],
        unique=False,
    )
    op.drop_index("ix_analysis_cycles_cursor", table_name="analysis_cycles")
    op.create_index(
        "ix_analysis_cycles_as_of",
        "analysis_cycles",
        ["as_of"],
        unique=False,
    )
