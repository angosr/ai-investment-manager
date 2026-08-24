from __future__ import annotations

from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.kernel.identity import content_hash
from investment_manager.portfolio.tables import (
    portfolio_account_snapshots,
    portfolio_targets,
)
from investment_manager.risk.portfolio import (
    ApprovedPortfolioTarget,
    ApprovedSleeve,
    PortfolioHoldingRiskReview,
    PortfolioRiskDecision,
    RiskReductionAuthorization,
)
from investment_manager.risk.tables import (
    portfolio_holding_risk_reviews,
    portfolio_risk_decisions,
    risk_execution_authorizations,
)


class PortfolioRiskStore(Protocol):
    def record(self, decision: PortfolioRiskDecision) -> bool: ...

    def for_target(self, target_id: str) -> PortfolioRiskDecision | None: ...

    def for_approved_targets(
        self,
        approved_target_ids: tuple[str, ...],
    ) -> dict[str, PortfolioRiskDecision]: ...

    def execution_authorizations(
        self,
        authorization_ids: tuple[str, ...],
    ) -> dict[str, tuple[ApprovedSleeve, ...]]: ...

    def execution_authorization(
        self,
        authorization_id: str,
    ) -> ApprovedPortfolioTarget | RiskReductionAuthorization | None: ...

    def record_holding_review(self, review: PortfolioHoldingRiskReview) -> bool: ...


class SqlPortfolioRiskStore:
    """Immutable authorization ledger bound to one persisted PortfolioTarget."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, decision: PortfolioRiskDecision) -> bool:
        try:
            with self._engine.begin() as connection:
                target_hash = connection.execute(
                    select(portfolio_targets.c.target_hash).where(
                        portfolio_targets.c.target_id == decision.target_id
                    )
                ).scalar_one_or_none()
                if target_hash != decision.target_hash:
                    raise ValueError("RiskDecision 缺少匹配的权威 PortfolioTarget")
                if decision.approved_target is not None:
                    self._insert_authorization(
                        connection,
                        authorization_id=decision.approved_target.approved_target_id,
                        source_type="PORTFOLIO_TARGET",
                        source_id=decision.decision_id,
                        authorized_at=decision.decided_at,
                        payload=decision.approved_target,
                    )
                connection.execute(
                    insert(portfolio_risk_decisions).values(
                        decision_id=decision.decision_id,
                        target_id=decision.target_id,
                        approved_target_id=(
                            decision.approved_target.approved_target_id
                            if decision.approved_target is not None
                            else None
                        ),
                        outcome=decision.outcome.value,
                        decided_at=decision.decided_at,
                        decision_hash=content_hash(decision),
                        payload=decision.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.for_target(decision.target_id)
            if existing != decision:
                raise ValueError("Risk target 已存在且决策内容不同") from None
            return False

    def record_holding_review(self, review: PortfolioHoldingRiskReview) -> bool:
        try:
            with self._engine.begin() as connection:
                account = connection.execute(
                    select(
                        portfolio_account_snapshots.c.snapshot_hash,
                        portfolio_account_snapshots.c.portfolio_id,
                    ).where(portfolio_account_snapshots.c.snapshot_id == review.account_snapshot_id)
                ).one_or_none()
                if account is None or (
                    account.snapshot_hash != review.account_snapshot_hash
                    or account.portfolio_id != review.portfolio_id
                ):
                    raise ValueError("Holding Risk 缺少匹配的权威账户快照")
                if review.reduction_authorization is not None:
                    self._insert_authorization(
                        connection,
                        authorization_id=review.reduction_authorization.authorization_id,
                        source_type="HOLDING_REVIEW",
                        source_id=review.review_id,
                        authorized_at=review.reviewed_at,
                        payload=review.reduction_authorization,
                    )
                connection.execute(
                    insert(portfolio_holding_risk_reviews).values(
                        review_id=review.review_id,
                        account_snapshot_id=review.account_snapshot_id,
                        policy_version=review.policy_version,
                        portfolio_id=review.portfolio_id,
                        reviewed_at=review.reviewed_at,
                        outcome=review.outcome.value,
                        review_hash=content_hash(review),
                        payload=review.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.holding_review(
                account_snapshot_id=review.account_snapshot_id,
                policy_version=review.policy_version,
            )
            if existing != review:
                raise ValueError("Holding Risk 账户复核事实已存在且内容不同") from None
            return False

    def holding_review(
        self,
        *,
        account_snapshot_id: str,
        policy_version: str,
    ) -> PortfolioHoldingRiskReview | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_holding_risk_reviews.c.payload).where(
                    portfolio_holding_risk_reviews.c.account_snapshot_id == account_snapshot_id,
                    portfolio_holding_risk_reviews.c.policy_version == policy_version,
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioHoldingRiskReview.model_validate(payload)

    def decision(self, decision_id: str) -> PortfolioRiskDecision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_risk_decisions.c.payload).where(
                    portfolio_risk_decisions.c.decision_id == decision_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioRiskDecision.model_validate(payload)

    def for_target(self, target_id: str) -> PortfolioRiskDecision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_risk_decisions.c.payload).where(
                    portfolio_risk_decisions.c.target_id == target_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioRiskDecision.model_validate(payload)

    def for_approved_targets(
        self,
        approved_target_ids: tuple[str, ...],
    ) -> dict[str, PortfolioRiskDecision]:
        approved_target_ids = tuple(sorted(set(approved_target_ids)))
        if not approved_target_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    portfolio_risk_decisions.c.approved_target_id,
                    portfolio_risk_decisions.c.payload,
                ).where(portfolio_risk_decisions.c.approved_target_id.in_(approved_target_ids))
            ).all()
        return {
            row.approved_target_id: PortfolioRiskDecision.model_validate(row.payload)
            for row in rows
        }

    def execution_authorizations(
        self,
        authorization_ids: tuple[str, ...],
    ) -> dict[str, tuple[ApprovedSleeve, ...]]:
        authorization_ids = tuple(sorted(set(authorization_ids)))
        if not authorization_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    risk_execution_authorizations.c.authorization_id,
                    risk_execution_authorizations.c.source_type,
                    risk_execution_authorizations.c.payload,
                ).where(
                    risk_execution_authorizations.c.authorization_id.in_(authorization_ids)
                )
            ).all()
        result: dict[str, tuple[ApprovedSleeve, ...]] = {}
        for row in rows:
            if row.source_type == "PORTFOLIO_TARGET":
                authorization = ApprovedPortfolioTarget.model_validate(row.payload)
            elif row.source_type == "HOLDING_REVIEW":
                authorization = RiskReductionAuthorization.model_validate(row.payload)
            else:
                raise ValueError("未知 Risk execution authorization 类型")
            result[row.authorization_id] = authorization.sleeves
        return result

    def execution_authorization(
        self,
        authorization_id: str,
    ) -> ApprovedPortfolioTarget | RiskReductionAuthorization | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    risk_execution_authorizations.c.source_type,
                    risk_execution_authorizations.c.payload,
                ).where(
                    risk_execution_authorizations.c.authorization_id == authorization_id
                )
            ).one_or_none()
        if row is None:
            return None
        if row.source_type == "PORTFOLIO_TARGET":
            return ApprovedPortfolioTarget.model_validate(row.payload)
        if row.source_type == "HOLDING_REVIEW":
            return RiskReductionAuthorization.model_validate(row.payload)
        raise ValueError("未知 Risk execution authorization 类型")

    @staticmethod
    def _insert_authorization(
        connection,
        *,
        authorization_id: str,
        source_type: str,
        source_id: str,
        authorized_at,
        payload: ApprovedPortfolioTarget | RiskReductionAuthorization,
    ) -> None:
        connection.execute(
            insert(risk_execution_authorizations).values(
                authorization_id=authorization_id,
                source_type=source_type,
                source_id=source_id,
                authorized_at=authorized_at,
                authorization_hash=content_hash(payload),
                payload=payload.model_dump(mode="json"),
            )
        )
