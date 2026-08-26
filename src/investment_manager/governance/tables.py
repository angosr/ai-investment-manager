from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
)

from investment_manager.platform.database import metadata

release_manifests = Table(
    "release_manifests",
    metadata,
    Column("manifest_id", String(128), primary_key=True),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

evaluation_plans = Table(
    "evaluation_plans",
    metadata,
    Column("plan_id", String(128), primary_key=True),
    Column("registered_at", DateTime(timezone=True), nullable=False),
    Column("base_manifest_id", String(128), nullable=False),
    Column("regression_suite_version", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
)

blind_evaluation_claims = Table(
    "blind_evaluation_claims",
    metadata,
    Column("plan_id", ForeignKey("evaluation_plans.plan_id"), primary_key=True),
    Column("query_id", String(128), nullable=False, unique=True),
    Column("blind_scope_id", String(128), nullable=False, unique=True),
    Column("blind_symbol", String(32), nullable=False),
    Column("blind_start", DateTime(timezone=True), nullable=False),
    Column("blind_end", DateTime(timezone=True), nullable=False),
    Column("source_evaluation_id", String(128), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("result_id", String(128), nullable=True),
    Column("result_hash", String(64), nullable=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_blind_evaluation_claim_symbol_window",
    blind_evaluation_claims.c.blind_symbol,
    blind_evaluation_claims.c.blind_start,
    blind_evaluation_claims.c.blind_end,
)

failed_experiment_records = Table(
    "failed_experiments",
    metadata,
    Column("experiment_id", String(128), primary_key=True),
    Column("hypothesis_fingerprint", String(64), nullable=False),
    Column("rejected_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_failed_experiment_fingerprint", failed_experiment_records.c.hypothesis_fingerprint)

historical_capital_benchmark_points = Table(
    "historical_capital_benchmark_points",
    metadata,
    Column("point_id", String(128), primary_key=True),
    Column("policy_id", String(128), nullable=False),
    Column(
        "account_snapshot_id",
        ForeignKey("portfolio_account_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column("revision", Integer, nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("source_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)

world_model_ablation_assignments = Table(
    "world_model_ablation_assignments",
    metadata,
    Column("assignment_id", String(128), primary_key=True),
    Column("plan_id", ForeignKey("evaluation_plans.plan_id"), nullable=False),
    # The formal forecast id is deterministic from slot + behavior and is
    # reserved before either paired AI call starts.  It therefore cannot carry
    # an immediate FK to a Forecast row that does not exist yet.
    Column("formal_forecast_id", String(128), nullable=False),
    Column("decision_slot_id", String(128), nullable=False),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    Column("completion_deadline_at", DateTime(timezone=True), nullable=False),
    Column("evaluation_at", DateTime(timezone=True), nullable=False),
    Column("control_behavior_hash", String(64), nullable=False),
    Column("source_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "uq_world_model_ablation_plan_forecast",
    world_model_ablation_assignments.c.plan_id,
    world_model_ablation_assignments.c.formal_forecast_id,
    unique=True,
)
Index(
    "ix_world_model_ablation_plan_slot",
    world_model_ablation_assignments.c.plan_id,
    world_model_ablation_assignments.c.evaluation_at,
)

world_model_ablation_results = Table(
    "world_model_ablation_results",
    metadata,
    Column("result_id", String(128), primary_key=True),
    Column(
        "assignment_id",
        ForeignKey("world_model_ablation_assignments.assignment_id"),
        nullable=False,
        unique=True,
    ),
    Column("status", String(32), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "uq_historical_capital_benchmark_policy_account",
    historical_capital_benchmark_points.c.policy_id,
    historical_capital_benchmark_points.c.account_snapshot_id,
    unique=True,
)
Index(
    "ix_historical_capital_benchmark_policy_revision",
    historical_capital_benchmark_points.c.policy_id,
    historical_capital_benchmark_points.c.revision,
)
