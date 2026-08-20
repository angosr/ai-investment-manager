"""Database tables owned by the point-in-time state domain."""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
)

from investment_manager.platform.database import metadata

decision_packets = Table(
    "decision_packets",
    metadata,
    Column("packet_id", String(128), primary_key=True),
    Column("analysis_scope", String(128), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_decision_packets_scope_as_of",
    decision_packets.c.analysis_scope,
    decision_packets.c.as_of,
)

state_evidence_snapshots = Table(
    "state_evidence_snapshots",
    metadata,
    Column("evidence_ref", String(64), primary_key=True),
    Column("evidence_kind", String(32), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

canonical_fact_revisions = Table(
    "canonical_fact_revisions",
    metadata,
    Column("revision_id", String(128), primary_key=True),
    Column("fact_id", String(128), nullable=False),
    Column(
        "previous_revision_id",
        ForeignKey("canonical_fact_revisions.revision_id"),
        nullable=True,
    ),
    Column("projection_version", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("revision_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "fact_id",
        "observed_at",
        name="uq_canonical_fact_revision_time",
    ),
)

canonical_fact_revision_sources = Table(
    "canonical_fact_revision_sources",
    metadata,
    Column(
        "revision_id",
        ForeignKey("canonical_fact_revisions.revision_id"),
        nullable=False,
    ),
    Column(
        "observation_id",
        ForeignKey("source_observations.observation_id"),
        nullable=False,
    ),
    PrimaryKeyConstraint("revision_id", "observation_id"),
)

state_snapshots = Table(
    "state_snapshots",
    metadata,
    Column("state_id", String(128), primary_key=True),
    Column("projection_version", String(128), nullable=False),
    Column("analysis_scope", String(128), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "analysis_scope",
        "projection_version",
        "as_of",
        name="uq_state_snapshot_scope_projection_time",
    ),
)

material_deltas = Table(
    "material_deltas",
    metadata,
    Column("delta_id", String(128), primary_key=True),
    Column("policy_version", String(128), nullable=False),
    Column("analysis_scope", String(128), nullable=False),
    Column(
        "previous_state_id",
        ForeignKey("state_snapshots.state_id"),
        nullable=False,
    ),
    Column(
        "current_state_id",
        ForeignKey("state_snapshots.state_id"),
        nullable=False,
    ),
    Column("category", String(32), nullable=False),
    Column("materiality", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "current_state_id",
        "policy_version",
        "category",
        name="uq_material_delta_state_policy_category",
    ),
)
Index(
    "ix_material_deltas_scope_observed",
    material_deltas.c.analysis_scope,
    material_deltas.c.observed_at,
)
Index("ix_material_deltas_expires", material_deltas.c.expires_at)
