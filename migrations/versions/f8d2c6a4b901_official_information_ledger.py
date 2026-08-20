"""add immutable official information ledger

Revision ID: f8d2c6a4b901
Revises: f1c7a4d8e209
Create Date: 2026-08-20 12:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8d2c6a4b901"
down_revision: str | Sequence[str] | None = "f1c7a4d8e209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_observations",
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_record_id", sa.String(length=2000), nullable=False),
        sa.Column("record_kind", sa.String(length=64), nullable=False),
        sa.Column("source_tier", sa.String(length=32), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "source_id",
            "source_record_id",
            "observed_at",
            name="uq_source_observation_record_time",
        ),
    )
    op.create_index(
        "ix_source_observations_record_observed",
        "source_observations",
        ["source_id", "source_record_id", "observed_at"],
    )
    op.create_table(
        "market_calendar_event_revisions",
        sa.Column("revision_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("previous_revision_id", sa.String(length=128), nullable=True),
        sa.Column("source_observation_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_record_id", sa.String(length=2000), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scheduled_release_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["previous_revision_id"],
            ["market_calendar_event_revisions.revision_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_observation_id"],
            ["source_observations.observation_id"],
        ),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("source_observation_id"),
        sa.UniqueConstraint(
            "event_id",
            "observed_at",
            name="uq_market_calendar_event_revision_time",
        ),
    )
    op.create_index(
        "ix_market_calendar_event_revisions_event_observed",
        "market_calendar_event_revisions",
        ["event_id", "observed_at"],
    )
    op.create_index(
        "ix_market_calendar_event_revisions_release",
        "market_calendar_event_revisions",
        ["scheduled_release_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_calendar_event_revisions_release",
        table_name="market_calendar_event_revisions",
    )
    op.drop_index(
        "ix_market_calendar_event_revisions_event_observed",
        table_name="market_calendar_event_revisions",
    )
    op.drop_table("market_calendar_event_revisions")
    op.drop_index(
        "ix_source_observations_record_observed",
        table_name="source_observations",
    )
    op.drop_table("source_observations")
