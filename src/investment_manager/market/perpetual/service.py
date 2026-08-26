from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.perpetual.client import BinanceUsdmRestClient
from investment_manager.market.perpetual.models import perpetual_product_rule_content_hash
from investment_manager.market.policy import MarketDataPolicy
from investment_manager.market.repository import MarketDataStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PerpetualMarketHealth:
    refresh_count: int = 0
    quote_refresh_count: int = 0
    state_count: int = 0
    quote_count: int = 0
    settlement_count: int = 0
    schedule_count: int = 0
    product_rule_count: int = 0
    last_refresh_at: datetime | None = None
    last_quote_refresh_at: datetime | None = None
    last_error_class: str | None = None
    product_rule_error_class: str | None = None
    last_quote_error_class: str | None = None


@dataclass(frozen=True, slots=True)
class PerpetualRefreshResult:
    started_at: datetime
    completed_at: datetime
    succeeded: bool
    latest_publication_at: datetime | None = None
    observation_count: int = 0
    changed_count: int = 0
    error_class: str | None = None


class BinancePerpetualMarketService:
    """Collect recoverable derivative facts without trading or trigger authority."""

    def __init__(
        self,
        *,
        policy: MarketDataPolicy,
        client: BinanceUsdmRestClient,
        store: MarketDataStore,
        refresh_observer: Callable[[PerpetualRefreshResult], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._policy = policy
        self._client = client
        self._store = store
        self._refresh_observer = refresh_observer
        self._clock = clock
        self._expected_funding_at: dict[str, datetime] = {}
        self.health = PerpetualMarketHealth()

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(
            self._run_schedule(
                stop,
                operation=self.refresh,
                interval_seconds=self._policy.perpetual_poll_seconds,
                error_field="last_error_class",
                error_message="perpetual market state refresh failed",
            ),
            self._run_schedule(
                stop,
                operation=self.refresh_quotes,
                interval_seconds=self._policy.perpetual_quote_poll_seconds,
                error_field="last_quote_error_class",
                error_message="perpetual executable quote refresh failed",
            ),
        )

    async def _run_schedule(
        self,
        stop: asyncio.Event,
        *,
        operation: Callable[[], Awaitable[None]],
        interval_seconds: int,
        error_field: str,
        error_message: str,
    ) -> None:
        while not stop.is_set():
            try:
                await operation()
                setattr(self.health, error_field, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(self.health, error_field) != type(exc).__name__:
                    logger.exception(error_message)
                setattr(self.health, error_field, type(exc).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=interval_seconds,
                )

    async def refresh_quotes(self) -> None:
        await asyncio.gather(
            *(
                self._refresh_quote_instrument(item)
                for item in self._policy.perpetual_instruments
            )
        )
        self.health.quote_refresh_count += 1
        self.health.last_quote_refresh_at = require_utc(self._clock())

    async def _refresh_quote_instrument(self, instrument: InstrumentId) -> None:
        quote = await self._client.fetch_quote(instrument)
        if self._store.put_perpetual_quote(quote):
            self.health.quote_count += 1

    async def refresh(self) -> None:
        started_at = require_utc(self._clock())
        try:
            gathered = await asyncio.gather(
                self._refresh_schedule_if_required(),
                self._refresh_product_rules(),
                *(
                    self._refresh_state_instrument(item)
                    for item in self._policy.perpetual_instruments
                ),
                return_exceptions=True,
            )
            schedule_result, rule_result, *state_results = gathered
            for result in (schedule_result, *state_results):
                if isinstance(result, BaseException):
                    raise result
            schedule = schedule_result
            results = state_results
            if isinstance(rule_result, BaseException):
                if not isinstance(rule_result, Exception):
                    raise rule_result
                self.health.product_rule_error_class = type(rule_result).__name__
                logger.warning(
                    "perpetual product rules unavailable; product candidates fail closed",
                    exc_info=rule_result,
                )
                rules = None
            else:
                rules = rule_result
                self.health.product_rule_error_class = None
        except Exception as exc:
            if self._refresh_observer is not None:
                self._refresh_observer(
                    PerpetualRefreshResult(
                        started_at=started_at,
                        completed_at=max(require_utc(self._clock()), started_at),
                        succeeded=False,
                        error_class=type(exc).__name__,
                    )
                )
            raise
        completed_at = max(require_utc(self._clock()), started_at)
        refresh = PerpetualRefreshResult(
            started_at=started_at,
            completed_at=completed_at,
            succeeded=True,
            latest_publication_at=max(
                (
                    *(item[0] for item in results),
                    *(schedule[:1] if schedule is not None else ()),
                    *((rules[0],) if rules is not None else ()),
                ),
                default=None,
            ),
            observation_count=sum(item[1] for item in results)
            + (1 if schedule is not None else 0)
            + (rules[1] if rules is not None else 0),
            changed_count=sum(item[2] for item in results)
            + (int(schedule[1]) if schedule is not None else 0)
            + (rules[2] if rules is not None else 0),
        )
        if self._refresh_observer is not None:
            self._refresh_observer(refresh)
        self.health.refresh_count += 1
        self.health.last_refresh_at = completed_at

    async def _refresh_schedule_if_required(self) -> tuple[datetime, bool] | None:
        if not any(
            item.product == InstrumentProduct.TRADFI_PERPETUAL
            for item in self._policy.perpetual_instruments
        ):
            return None
        schedule = await self._client.fetch_trading_schedule()
        inserted = self._store.put_trading_schedule(schedule)
        if inserted:
            self.health.schedule_count += 1
        return schedule.observed_at, inserted

    async def _refresh_product_rules(self) -> tuple[datetime, int, int]:
        rules = await self._client.fetch_product_rules(
            self._policy.perpetual_instruments
        )
        if len(rules) != len(self._policy.perpetual_instruments):
            raise ValueError("Binance USD-M 产品规则没有完整覆盖配置 Instrument")
        changed = 0
        for item in rules:
            previous = self._store.latest_perpetual_product_rules(
                instrument=item.instrument,
                as_of=item.observed_at,
            )
            content_changed = previous is None or (
                perpetual_product_rule_content_hash(previous)
                != perpetual_product_rule_content_hash(item)
            )
            if self._store.put_perpetual_product_rules(item):
                self.health.product_rule_count += 1
            changed += int(content_changed)
        return (
            max(item.observed_at for item in rules),
            len(rules),
            changed,
        )

    async def _refresh_state_instrument(
        self,
        instrument: InstrumentId,
    ) -> tuple[datetime, int, int]:
        state = await self._client.fetch_market_state(instrument)
        changed_count = 0
        if self._store.put_perpetual_state(state):
            self.health.state_count += 1
            changed_count += 1
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
                    changed_count += 1
        self._expected_funding_at[instrument.key] = state.next_funding_time
        return (
            state.observed_at,
            1 + (len(settlements) if history_due else 0),
            changed_count,
        )
