"""Database tables owned by the information domain."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    UniqueConstraint,
    true,
)

from investment_manager.platform.database import metadata

source_observations = Table(
    "source_observations",
    metadata,
    Column("observation_id", String(128), primary_key=True),
    Column("source_id", String(128), nullable=False),
    Column("source_record_id", String(2_000), nullable=False),
    Column("record_kind", String(64), nullable=False),
    Column("source_tier", String(32), nullable=False),
    Column("source_published_at", DateTime(timezone=True), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "source_id",
        "source_record_id",
        "observed_at",
        name="uq_source_observation_record_time",
    ),
)

raw_source_payloads = Table(
    "raw_source_payloads",
    metadata,
    Column("payload_id", String(128), primary_key=True),
    Column("source_id", String(128), nullable=False),
    Column("source_url", String(2_000), nullable=False),
    Column("media_type", String(128), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("byte_count", Integer, nullable=False),
    Column("content", LargeBinary, nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_raw_source_payloads_source_observed",
    raw_source_payloads.c.source_id,
    raw_source_payloads.c.observed_at,
)

market_calendar_event_revisions = Table(
    "market_calendar_event_revisions",
    metadata,
    Column("revision_id", String(128), primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column(
        "previous_revision_id",
        ForeignKey("market_calendar_event_revisions.revision_id"),
        nullable=True,
    ),
    Column(
        "source_observation_id",
        ForeignKey("source_observations.observation_id"),
        nullable=False,
        unique=True,
    ),
    Column("source_id", String(128), nullable=False),
    Column("source_record_id", String(2_000), nullable=False),
    Column("status", String(32), nullable=False),
    Column("scheduled_release_at", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "event_id",
        "observed_at",
        name="uq_market_calendar_event_revision_time",
    ),
)
Index(
    "ix_market_calendar_event_revisions_release",
    market_calendar_event_revisions.c.scheduled_release_at,
)

normalized_events = Table(
    "normalized_events",
    metadata,
    Column("evidence_id", String(128), primary_key=True),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("source", String(128), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column(
        "expands_document_information",
        Boolean,
        nullable=False,
        server_default=true(),
    ),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("source", "content_hash", name="uq_normalized_event_source_hash"),
)
Index("ix_normalized_events_event_time", normalized_events.c.event_time)

source_poll_records = Table(
    "source_poll_records",
    metadata,
    Column("poll_id", String(128), primary_key=True),
    Column("source_stream_id", String(128), nullable=False),
    Column("domain", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_source_poll_records_stream_time",
    source_poll_records.c.source_stream_id,
    source_poll_records.c.completed_at,
)
