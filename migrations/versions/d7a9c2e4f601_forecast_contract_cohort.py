"""add source-independent forecast contracts and continuous cohort

Revision ID: d7a9c2e4f601
Revises: c4a8e2f7b901
Create Date: 2026-08-23 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a9c2e4f601"
down_revision: str | Sequence[str] | None = "c4a8e2f7b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "historical_assessment_view_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("already_priced", sa.String(length=32), nullable=False),
        sa.Column("uncertainty", sa.String(length=32), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("signal_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("directional_return_bps", sa.Numeric(38, 18), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("outcome_id"),
    )
    op.create_index(
        "ix_historical_assessment_view_outcomes_cohort",
        "historical_assessment_view_outcomes",
        [
            "analysis_behavior_hash",
            "asset",
            "symbol",
            "horizon_minutes",
            "evaluation_at",
        ],
    )
    op.execute(
        sa.text(
            "INSERT INTO historical_assessment_view_outcomes "
            "SELECT * FROM assessment_view_outcomes"
        )
    )
    op.drop_table("assessment_view_outcomes")
    with op.batch_alter_table("context_assessments") as batch_op:
        batch_op.drop_column("view_count")
    op.create_table(
        "forecast_contracts",
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("contract_version", sa.String(length=128), nullable=False),
        sa.Column("outcome_family_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("contract_id"),
    )
    op.create_index(
        "ix_forecast_contracts_family_version",
        "forecast_contracts",
        ["outcome_family_id", "contract_version"],
    )
    op.create_table(
        "forecast_producer_bindings",
        sa.Column("binding_id", sa.String(length=128), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("producer_kind", sa.String(length=32), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("producer_behavior_id", sa.String(length=128), nullable=False),
        sa.Column("permission", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["forecast_contracts.contract_id"]),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint(
            "contract_id",
            "producer_behavior_id",
            name="uq_forecast_binding_contract_behavior",
        ),
    )
    op.create_table(
        "forecast_decision_slots",
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("slot_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("information_cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completion_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["forecast_contracts.contract_id"]),
        sa.PrimaryKeyConstraint("slot_id"),
        sa.UniqueConstraint(
            "contract_id",
            "slot_as_of",
            name="uq_forecast_decision_slot_contract_time",
        ),
    )
    op.create_index(
        "ix_forecast_decision_slots_evaluation",
        "forecast_decision_slots",
        ["evaluation_at", "slot_id"],
    )
    op.create_table(
        "forecast_no_estimates",
        sa.Column("result_id", sa.String(length=128), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("producer_kind", sa.String(length=32), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("producer_behavior_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["forecast_contracts.contract_id"]),
        sa.ForeignKeyConstraint(["slot_id"], ["forecast_decision_slots.slot_id"]),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint(
            "slot_id",
            "producer_behavior_id",
            name="uq_forecast_no_estimate_slot_behavior",
        ),
    )
    op.create_index(
        "ix_forecast_no_estimates_behavior_time",
        "forecast_no_estimates",
        ["producer_behavior_id", "completed_at"],
    )
    op.create_table(
        "historical_opportunity_reviews",
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("world_model_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("review_id"),
    )
    op.create_table(
        "historical_opportunity_assessments",
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("opportunity_id", sa.String(length=128), nullable=False),
        sa.Column("world_model_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_behavior_hash", sa.String(length=64), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("assessment_id"),
    )
    op.execute(
        sa.text("INSERT INTO historical_opportunity_reviews SELECT * FROM opportunity_reviews")
    )
    op.execute(
        sa.text(
            "INSERT INTO historical_opportunity_assessments "
            "SELECT * FROM opportunity_assessments"
        )
    )
    op.drop_table("opportunity_assessments")
    op.drop_table("opportunity_reviews")
    op.create_table(
        "historical_forecasts",
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("producer_version", sa.String(length=128), nullable=False),
        sa.Column("forecast_family", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_forecast_id", sa.String(length=128), nullable=True),
        sa.Column("assessment_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("forecast_id"),
    )
    op.create_table(
        "historical_forecast_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gross_target_return_bps", sa.Numeric(38, 18), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("outcome_id"),
    )
    op.create_table(
        "historical_portfolio_target_forecasts",
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("target_id", "forecast_id"),
    )
    op.execute(sa.text("INSERT INTO historical_forecasts SELECT * FROM forecasts"))
    op.execute(
        sa.text(
            "INSERT INTO historical_forecast_outcomes SELECT * FROM forecast_outcomes"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO historical_portfolio_target_forecasts "
            "SELECT * FROM portfolio_target_forecasts"
        )
    )
    op.drop_table("portfolio_target_forecasts")
    op.drop_table("forecast_outcomes")
    op.drop_table("forecasts")
    op.create_table(
        "forecasts",
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("decision_slot_id", sa.String(length=128), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("producer_behavior_id", sa.String(length=128), nullable=False),
        sa.Column("outcome_family_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_forecast_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["base_forecast_id"], ["forecasts.forecast_id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["forecast_contracts.contract_id"]),
        sa.ForeignKeyConstraint(
            ["decision_slot_id"], ["forecast_decision_slots.slot_id"]
        ),
        sa.PrimaryKeyConstraint("forecast_id"),
        sa.UniqueConstraint(
            "decision_slot_id",
            "producer_behavior_id",
            "kind",
            name="uq_forecast_slot_behavior_kind",
        ),
    )
    op.create_index(
        "ix_forecasts_contract_slot",
        "forecasts",
        ["contract_id", "decision_slot_id"],
    )
    op.create_index(
        "ix_forecasts_producer_target",
        "forecasts",
        ["producer_id", "producer_behavior_id", "target_id", "available_at"],
    )
    op.create_table(
        "forecast_outcomes",
        sa.Column("outcome_id", sa.String(length=128), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("decision_slot_id", sa.String(length=128), nullable=False),
        sa.Column("evaluation_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gross_target_return_bps", sa.Numeric(38, 18), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["forecast_contracts.contract_id"]),
        sa.ForeignKeyConstraint(
            ["decision_slot_id"], ["forecast_decision_slots.slot_id"]
        ),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint(
            "decision_slot_id",
            "evaluation_version",
            name="uq_forecast_outcome_identity",
        ),
    )
    op.create_index(
        "ix_forecast_outcomes_cohort",
        "forecast_outcomes",
        ["evaluation_version", "evaluation_at", "decision_slot_id"],
    )
    op.create_table(
        "portfolio_target_forecasts",
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("forecast_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["forecast_id"],
            ["forecasts.forecast_id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["portfolio_targets.target_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("target_id", "forecast_id"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Forecast cohort hard migration archives immutable historical facts and "
        "cannot be downgraded without an explicit audited restore migration"
    )
