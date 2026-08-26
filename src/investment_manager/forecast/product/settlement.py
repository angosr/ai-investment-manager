"""Point-in-time settlement for deterministic product payoff projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from investment_manager.forecast.product.models import (
    ProductPayoffOutcome,
    ProductPayoffProjection,
)
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.results import ForecastOutcomeStatus
from investment_manager.forecast.settlement import (
    ForecastPayoffResolver,
    MarketFactsIncomplete,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import MarketDataStore


@dataclass(frozen=True, slots=True)
class ProductPayoffSettlementResult:
    settled: int = 0
    outcome_unavailable: int = 0
    pending: int = 0


@dataclass(slots=True)
class ProductPayoffOutcomeSettler:
    """Settle each legal product expression without creating another Forecast sample."""

    market: MarketDataStore
    store: SqlProductPayoffProjectionStore
    evaluation_version: str
    maximum_spot_age_seconds: int
    maximum_perpetual_age_seconds: int
    maximum_funding_gap_hours: int
    settlement_grace_minutes: int
    batch_size: int = 100

    def settle(self, *, as_of: datetime) -> ProductPayoffSettlementResult:
        now = require_utc(as_of)
        settled = unavailable = pending = 0
        for projection in self.store.pending_outcomes(
            evaluation_version=self.evaluation_version,
            due_at=now,
            limit=self.batch_size,
        ):
            try:
                outcome = self._outcome(projection=projection, settled_at=now)
            except MarketFactsIncomplete:
                if now - projection.evaluation_at < timedelta(
                    minutes=self.settlement_grace_minutes
                ):
                    pending += 1
                    continue
                outcome = self._unavailable(projection=projection, settled_at=now)
            inserted = int(self.store.record_outcome(outcome))
            settled += inserted * int(outcome.status == ForecastOutcomeStatus.SETTLED)
            unavailable += inserted * int(
                outcome.status == ForecastOutcomeStatus.OUTCOME_UNAVAILABLE
            )
        return ProductPayoffSettlementResult(
            settled=settled,
            outcome_unavailable=unavailable,
            pending=pending,
        )

    def _outcome(
        self,
        *,
        projection: ProductPayoffProjection,
        settled_at: datetime,
    ) -> ProductPayoffOutcome:
        leg = self._resolver().leg_outcome(
            leg=projection.target.legs[0],
            reference_price=projection.entry_anchor.price,
            outcome_start_at=projection.projected_at,
            evaluation_at=projection.evaluation_at,
            settled_at=settled_at,
        )
        return ProductPayoffOutcome(
            **self._common(projection=projection, settled_at=settled_at),
            status=ForecastOutcomeStatus.SETTLED,
            leg=leg,
            realized_gross_bps=leg.price_return_bps + leg.funding_return_bps,
            reason_code="EXECUTABLE_PRODUCT_PAYOFF_AVAILABLE",
        )

    def _unavailable(
        self,
        *,
        projection: ProductPayoffProjection,
        settled_at: datetime,
    ) -> ProductPayoffOutcome:
        return ProductPayoffOutcome(
            **self._common(projection=projection, settled_at=settled_at),
            status=ForecastOutcomeStatus.OUTCOME_UNAVAILABLE,
            reason_code="POINT_IN_TIME_MARKET_OR_FUNDING_FACTS_INCOMPLETE",
        )

    def _common(
        self,
        *,
        projection: ProductPayoffProjection,
        settled_at: datetime,
    ) -> dict[str, object]:
        return {
            "outcome_id": stable_id(
                "product_payoff_outcome",
                projection.projection_id,
                self.evaluation_version,
            ),
            "projection_id": projection.projection_id,
            "source_forecast_id": projection.source_forecast_id,
            "evaluation_version": self.evaluation_version,
            "projected_at": projection.projected_at,
            "evaluation_at": projection.evaluation_at,
            "settled_at": settled_at,
        }

    def _resolver(self) -> ForecastPayoffResolver:
        return ForecastPayoffResolver(
            market=self.market,
            maximum_spot_age_seconds=self.maximum_spot_age_seconds,
            maximum_perpetual_age_seconds=self.maximum_perpetual_age_seconds,
            maximum_funding_gap_hours=self.maximum_funding_gap_hours,
        )


__all__ = [
    "ProductPayoffOutcomeSettler",
    "ProductPayoffSettlementResult",
]
