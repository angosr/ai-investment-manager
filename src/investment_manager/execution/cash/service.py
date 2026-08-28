from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from investment_manager.execution.cash.models import CashYieldProductObservation
from investment_manager.execution.cash.policy import CashYieldEvidencePolicy
from investment_manager.execution.cash.repository import CashYieldObservationStore
from investment_manager.kernel.time import require_utc


class CashYieldObservationSource(Protocol):
    def fetch(self, *, preview_amount: Decimal) -> CashYieldProductObservation: ...


@dataclass(frozen=True, slots=True)
class CashYieldObservationResult:
    observation: CashYieldProductObservation
    recorded: bool
    refreshed: bool


class CashYieldEvidenceService:
    def __init__(
        self,
        *,
        policy: CashYieldEvidencePolicy,
        source: CashYieldObservationSource,
        store: CashYieldObservationStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._source = source
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def observe(self, *, preview_amount: Decimal) -> CashYieldObservationResult:
        now = require_utc(self._clock())
        latest = self._store.latest(
            product_id=self._policy.product_id,
            asset=self._policy.asset,
            visible_at=now,
        )
        if latest is not None and (
            now - latest.available_at
        ).total_seconds() < self._policy.refresh_seconds:
            return CashYieldObservationResult(
                observation=latest,
                recorded=False,
                refreshed=False,
            )
        observation = self._source.fetch(preview_amount=preview_amount)
        if observation.policy_version != self._policy.version:
            raise ValueError("现金收益产品观察与启用政策版本不一致")
        return CashYieldObservationResult(
            observation=observation,
            recorded=self._store.put(observation),
            refreshed=True,
        )
