"""add append-only world mechanism observations

Revision ID: c4a8e2f7b901
Revises: b1e7c4a9d205
Create Date: 2026-08-23 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a8e2f7b901"
down_revision: str | Sequence[str] | None = "b1e7c4a9d205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_mechanism_observations",
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("mechanism_id", sa.String(length=128), nullable=False),
        sa.Column("test_id", sa.String(length=128), nullable=False),
        sa.Column("packet_id", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["context_assessments.assessment_id"]),
        sa.ForeignKeyConstraint(["packet_id"], ["decision_packets.packet_id"]),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "assessment_id",
            "test_id",
            "packet_id",
            name="uq_context_mechanism_observation",
        ),
    )
    op.create_index(
        "ix_context_mechanism_observations_test_time",
        "context_mechanism_observations",
        ["assessment_id", "test_id", "observed_at"],
    )
    op.create_table(
        "opportunity_reviews",
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("world_model_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint("content_hash"),
    )
    op.create_index(
        "ix_opportunity_reviews_opportunity_time",
        "opportunity_reviews",
        ["opportunity_id", "created_at"],
    )
    op.create_table(
        "opportunity_assessments",
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("world_model_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["opportunity_reviews.review_id"]),
        sa.PrimaryKeyConstraint("assessment_id"),
        sa.UniqueConstraint(
            "review_id",
            "analysis_behavior_hash",
            name="uq_opportunity_assessment_review_behavior",
        ),
    )
    op.create_index(
        "ix_opportunity_assessments_opportunity_time",
        "opportunity_assessments",
        ["opportunity_id", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opportunity_assessments_opportunity_time",
        table_name="opportunity_assessments",
    )
    op.drop_table("opportunity_assessments")
    op.drop_index(
        "ix_opportunity_reviews_opportunity_time",
        table_name="opportunity_reviews",
    )
    op.drop_table("opportunity_reviews")
    op.drop_index(
        "ix_context_mechanism_observations_test_time",
        table_name="context_mechanism_observations",
    )
    op.drop_table("context_mechanism_observations")
