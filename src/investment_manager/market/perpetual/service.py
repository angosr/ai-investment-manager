from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId
from investment_manager.market.perpetual.client import BinanceUsdmRestClient
from investment_manager.market.policy import MarketDataPolicy
from investment_manager.market.repository import MarketDataStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PerpetualMarketHealth:
    refresh_count: int = 0
    state_count: int = 0
    settlement_count: int = 0
    last_refresh_at: datetime | None = None
    last_error_class: str | None = None


class BinancePerpetualMarketService:
    """Collect recoverable derivative facts without trading or trigger authority."""

    def __init__(
        self,
        *,
        policy: MarketDataPolicy,
        client: BinanceUsdmRestClient,
        store: MarketDataStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._policy = policy
        self._client = client
        self._store = store
        self._clock = clock
        self._expected_funding_at: dict[str, datetime] = {}
        self.health = PerpetualMarketHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.refresh()
                self.health.last_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("perpetual market refresh failed")
                self.health.last_error_class = type(exc).__name__
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self._policy.perpetual_poll_seconds,
                )

    async def refresh(self) -> None:
        await asyncio.gather(
            *(self._refresh_instrument(item) for item in self._policy.perpetual_instruments)
        )
        self.health.refresh_count += 1
        self.health.last_refresh_at = require_utc(self._clock())

    async def _refresh_instrument(self, instrument: InstrumentId) -> None:
        state = await self._client.fetch_market_state(instrument)
        if self._store.put_perpetual_state(state):
            self.health.state_count += 1
        previous_funding_at = self._expected_funding_at.get(instrument.key)
        history_due = previous_funding_at is None or state.observed_at >= previous_funding_at
        if history_due:
            start = state.observed_at - timedelta(hours=self._policy.funding_history_lookback_hours)
            settlements = await self._client.fetch_funding_settlements(
                instrument,
                start=start,
                end=state.observed_at,
            )
            for settlement in settlements:
                if self._store.put_funding_settlement(settlement):
                    self.health.settlement_count += 1
        self._expected_funding_at[instrument.key] = state.next_funding_time
