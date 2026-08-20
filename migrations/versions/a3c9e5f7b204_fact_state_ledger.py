"""add point-in-time fact state ledger

Revision ID: a3c9e5f7b204
Revises: f8d2c6a4b901
Create Date: 2026-08-20 12:36:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e5f7b204"
down_revision: str | Sequence[str] | None = "f8d2c6a4b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_source_observations_record_observed",
        table_name="source_observations",
    )
    op.drop_index(
        "ix_market_calendar_event_revisions_event_observed",
        table_name="market_calendar_event_revisions",
    )
    op.create_table(
        "canonical_fact_revisions",
        sa.Column("revision_id", sa.String(length=128), nullable=False),
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("previous_revision_id", sa.String(length=128), nullable=True),
        sa.Column("projection_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["previous_revision_id"],
            ["canonical_fact_revisions.revision_id"],
        ),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint(
            "fact_id",
            "observed_at",
            name="uq_canonical_fact_revision_time",
        ),
    )
    op.create_table(
        "canonical_fact_revision_sources",
        sa.Column("revision_id", sa.String(length=128), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["canonical_fact_revisions.revision_id"],
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["source_observations.observation_id"],
        ),
        sa.PrimaryKeyConstraint("revision_id", "observation_id"),
    )
    op.create_table(
        "state_snapshots",
        sa.Column("state_id", sa.String(length=128), nullable=False),
        sa.Column("projection_version", sa.String(length=128), nullable=False),
        sa.Column("analysis_scope", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("state_id"),
        sa.UniqueConstraint("content_hash"),
        sa.UniqueConstraint(
            "analysis_scope",
            "projection_version",
            "as_of",
            name="uq_state_snapshot_scope_projection_time",
        ),
    )
    op.create_table(
        "material_deltas",
        sa.Column("delta_id", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("analysis_scope", sa.String(length=128), nullable=False),
        sa.Column("previous_state_id", sa.String(length=128), nullable=False),
        sa.Column("current_state_id", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("materiality", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["current_state_id"], ["state_snapshots.state_id"]),
        sa.ForeignKeyConstraint(["previous_state_id"], ["state_snapshots.state_id"]),
        sa.PrimaryKeyConstraint("delta_id"),
        sa.UniqueConstraint("content_hash"),
        sa.UniqueConstraint(
            "current_state_id",
            "policy_version",
            "category",
            name="uq_material_delta_state_policy_category",
        ),
    )
    op.create_index(
        "ix_material_deltas_scope_observed",
        "material_deltas",
        ["analysis_scope", "observed_at"],
    )
    op.create_index(
        "ix_material_deltas_expires",
        "material_deltas",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_deltas_expires", table_name="material_deltas")
    op.drop_index(
        "ix_material_deltas_scope_observed",
        table_name="material_deltas",
    )
    op.drop_table("material_deltas")
    op.drop_table("state_snapshots")
    op.drop_table("canonical_fact_revision_sources")
    op.drop_table("canonical_fact_revisions")
    op.create_index(
        "ix_market_calendar_event_revisions_event_observed",
        "market_calendar_event_revisions",
        ["event_id", "observed_at"],
    )
    op.create_index(
        "ix_source_observations_record_observed",
        "source_observations",
        ["source_id", "source_record_id", "observed_at"],
    )
