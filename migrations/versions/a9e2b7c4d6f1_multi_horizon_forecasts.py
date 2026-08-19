"""multi horizon forecasts

Revision ID: a9e2b7c4d6f1
Revises: f6c1a8d2e4b7
Create Date: 2026-08-19 15:14:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9e2b7c4d6f1"
down_revision: str | Sequence[str] | None = "f6c1a8d2e4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    op.add_column(
        "analysis_proposals",
        sa.Column("forecast_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "analysis_forecast_outcomes",
        sa.Column("view_horizon_minutes", sa.Integer(), nullable=True),
    )

    bind = op.get_bind()
    proposal_rows = bind.execute(
        sa.text("SELECT proposal_id, payload FROM analysis_proposals")
    ).mappings()
    for row in proposal_rows:
        payload = _json_payload(row["payload"])
        forecasts = payload.get("forecasts")
        count = len(forecasts) if isinstance(forecasts, list) and forecasts else 1
        bind.execute(
            sa.text(
                "UPDATE analysis_proposals SET forecast_count = :count "
                "WHERE proposal_id = :proposal_id"
            ),
            {"count": count, "proposal_id": row["proposal_id"]},
        )

    outcome_rows = bind.execute(
        sa.text("SELECT outcome_id, payload FROM analysis_forecast_outcomes")
    ).mappings()
    for row in outcome_rows:
        payload = _json_payload(row["payload"])
        horizon = int(payload.get("view_horizon_minutes", 60))
        bind.execute(
            sa.text(
                "UPDATE analysis_forecast_outcomes "
                "SET view_horizon_minutes = :horizon WHERE outcome_id = :outcome_id"
            ),
            {"horizon": horizon, "outcome_id": row["outcome_id"]},
        )

    with op.batch_alter_table(
        "analysis_proposals", naming_convention=_NAMING_CONVENTION
    ) as batch:
        batch.alter_column(
            "forecast_count", existing_type=sa.Integer(), nullable=False
        )

    old_constraint = _proposal_only_constraint_name(bind)
    with op.batch_alter_table(
        "analysis_forecast_outcomes", naming_convention=_NAMING_CONVENTION
    ) as batch:
        batch.drop_constraint(old_constraint, type_="unique")
        batch.alter_column(
            "view_horizon_minutes", existing_type=sa.Integer(), nullable=False
        )
        batch.create_unique_constraint(
            "uq_analysis_forecast_outcomes_proposal_horizon",
            ["proposal_id", "view_horizon_minutes"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "SELECT proposal_id FROM analysis_forecast_outcomes "
            "GROUP BY proposal_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "多周期预测已产生多个结果，拒绝通过丢数据降级到单周期 Schema"
        )

    with op.batch_alter_table(
        "analysis_forecast_outcomes", naming_convention=_NAMING_CONVENTION
    ) as batch:
        batch.drop_constraint(
            "uq_analysis_forecast_outcomes_proposal_horizon", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_analysis_forecast_outcomes_proposal", ["proposal_id"]
        )
        batch.drop_column("view_horizon_minutes")
    with op.batch_alter_table(
        "analysis_proposals", naming_convention=_NAMING_CONVENTION
    ) as batch:
        batch.drop_column("forecast_count")


def _proposal_only_constraint_name(bind) -> str:
    constraints = sa.inspect(bind).get_unique_constraints(
        "analysis_forecast_outcomes"
    )
    matches = [
        item
        for item in constraints
        if item.get("column_names") == ["proposal_id"]
    ]
    if len(matches) != 1:
        raise RuntimeError("无法唯一识别旧的单周期 Proposal 唯一约束")
    return str(matches[0].get("name") or "uq_analysis_forecast_outcomes_proposal_id")


def _json_payload(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("预测迁移遇到非法 JSON Payload")
    return value
