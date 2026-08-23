"""archive legacy world-model payloads from the active v2 ledger

Revision ID: f3a7c9e2d501
Revises: e2f6a8c4d901
Create Date: 2026-08-23 15:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c9e2d501"
down_revision: str | Sequence[str] | None = "e2f6a8c4d901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "historical_context_assessments",
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("packet_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_scope", sa.String(length=128), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("assessment_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO historical_context_assessments "
            "SELECT assessment_id, packet_id, analysis_scope, available_at, "
            "analysis_behavior_hash, payload FROM context_assessments "
            "WHERE COALESCE(payload ->> 'schema_version', '') "
            "<> 'world-model-assessment-v2'"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM context_assessments "
            "WHERE COALESCE(payload ->> 'schema_version', '') "
            "<> 'world-model-assessment-v2'"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "WorldModel v2 hard migration archives incompatible payloads and cannot "
        "be downgraded without an explicit audited restore migration"
    )
