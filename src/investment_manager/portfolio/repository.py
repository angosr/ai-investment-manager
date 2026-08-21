from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import Protocol

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.models import CalibratedForecast, ForecastKind
from investment_manager.forecast.tables import forecasts
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    PortfolioPerformanceInterval,
    PortfolioTarget,
)
from investment_manager.portfolio.rebalance import PortfolioRebalancePeriod
from investment_manager.portfolio.tables import (
    portfolio_account_snapshots,
    portfolio_performance_intervals,
    portfolio_rebalance_periods,
    portfolio_target_forecasts,
    portfolio_targets,
)


class PortfolioStore(Protocol):
    def record_account(self, account: PortfolioAccountSnapshot) -> bool: ...

    def record_target(self, target: PortfolioTarget) -> bool: ...

    def account_for_cycle(
        self,
        *,
        cycle_id: str,
        portfolio_id: str,
    ) -> PortfolioAccountSnapshot | None: ...

    def latest_account(
        self,
        *,
        portfolio_id: str,
        as_of: datetime,
    ) -> PortfolioAccountSnapshot | None: ...

    def rebalance_period(
        self,
        *,
        portfolio_id: str,
        policy_version: str,
        period_start: datetime,
    ) -> PortfolioRebalancePeriod | None: ...

    def claim_rebalance_period(
        self,
        period: PortfolioRebalancePeriod,
    ) -> PortfolioRebalancePeriod: ...


class SqlPortfolioStore:
    """Immutable account/target ledger; retries must reproduce exact facts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_account(self, account: PortfolioAccountSnapshot) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(portfolio_account_snapshots).values(
                        snapshot_id=account.snapshot_id,
                        cycle_id=account.cycle_id,
                        portfolio_id=account.portfolio_id,
                        revision=account.revision,
                        as_of=account.as_of,
                        observed_at=account.observed_at,
                        snapshot_hash=content_hash(account),
                        payload={
                            **account.model_dump(mode="json"),
                            "revision": account.revision,
                        },
                    )
                )
            return True
        except IntegrityError:
            existing = self.account_for_cycle(
                cycle_id=account.cycle_id,
                portfolio_id=account.portfolio_id,
            )
            if existing != account:
                raise ValueError("Portfolio account cycle 已存在且内容不同") from None
            return False

    def account(self, snapshot_id: str) -> PortfolioAccountSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_account_snapshots.c.payload).where(
                    portfolio_account_snapshots.c.snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioAccountSnapshot.model_validate(payload)

    def account_for_cycle(
        self,
        *,
        cycle_id: str,
        portfolio_id: str,
    ) -> PortfolioAccountSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_account_snapshots.c.payload).where(
                    portfolio_account_snapshots.c.cycle_id == cycle_id,
                    portfolio_account_snapshots.c.portfolio_id == portfolio_id,
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioAccountSnapshot.model_validate(payload)

    def latest_account(
        self,
        *,
        portfolio_id: str,
        as_of: datetime,
    ) -> PortfolioAccountSnapshot | None:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_account_snapshots.c.payload)
                .where(
                    portfolio_account_snapshots.c.portfolio_id == portfolio_id,
                    portfolio_account_snapshots.c.as_of <= as_of,
                )
                .order_by(
                    portfolio_account_snapshots.c.as_of.desc(),
                    portfolio_account_snapshots.c.revision.desc(),
                    portfolio_account_snapshots.c.snapshot_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else PortfolioAccountSnapshot.model_validate(payload)

    def record_target(self, target: PortfolioTarget) -> bool:
        try:
            with self._engine.begin() as connection:
                self._validate_target_dependencies(connection, target)
                connection.execute(
                    insert(portfolio_targets).values(
                        target_id=target.target_id,
                        cycle_id=target.cycle_id,
                        portfolio_id=target.portfolio_id,
                        account_snapshot_id=target.account_snapshot_id,
                        as_of=target.as_of,
                        valid_until=target.valid_until,
                        target_hash=content_hash(target),
                        payload=target.model_dump(mode="json"),
                    )
                )
                forecast_ids = target.considered_forecast_ids
                if forecast_ids:
                    connection.execute(
                        insert(portfolio_target_forecasts),
                        [
                            {
                                "target_id": target.target_id,
                                "forecast_id": forecast_id,
                            }
                            for forecast_id in forecast_ids
                        ],
                    )
            return True
        except IntegrityError:
            existing = self.target_for_cycle(target.cycle_id)
            if existing != target:
                raise ValueError("Portfolio target cycle 已存在且内容不同") from None
            return False

    def rebalance_period(
        self,
        *,
        portfolio_id: str,
        policy_version: str,
        period_start: datetime,
    ) -> PortfolioRebalancePeriod | None:
        period_start = require_utc(period_start)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_rebalance_periods.c.payload).where(
                    portfolio_rebalance_periods.c.portfolio_id == portfolio_id,
                    portfolio_rebalance_periods.c.policy_version == policy_version,
                    portfolio_rebalance_periods.c.period_start == period_start,
                )
            ).scalar_one_or_none()
        return (
            None
            if payload is None
            else PortfolioRebalancePeriod.model_validate(payload)
        )

    def claim_rebalance_period(
        self,
        period: PortfolioRebalancePeriod,
    ) -> PortfolioRebalancePeriod:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(portfolio_rebalance_periods).values(
                        period_id=period.period_id,
                        portfolio_id=period.portfolio_id,
                        policy_version=period.policy_version,
                        period_start=period.period_start,
                        period_end=period.period_end,
                        entry_window_end=period.entry_window_end,
                        decision_at=period.decision_at,
                        mode=period.mode.value,
                        candidate_forecast_id=period.candidate_forecast_id,
                        payload=period.model_dump(mode="json"),
                    )
                )
            return period
        except IntegrityError:
            existing = self.rebalance_period(
                portfolio_id=period.portfolio_id,
                policy_version=period.policy_version,
                period_start=period.period_start,
            )
            if existing is None:  # pragma: no cover - database contract guard
                raise
            return existing

    def target(self, target_id: str) -> PortfolioTarget | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_targets.c.payload).where(
                    portfolio_targets.c.target_id == target_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioTarget.model_validate(payload)

    def target_for_cycle(self, cycle_id: str) -> PortfolioTarget | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_targets.c.payload).where(
                    portfolio_targets.c.cycle_id == cycle_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioTarget.model_validate(payload)

    @staticmethod
    def _validate_target_dependencies(
        connection: Connection,
        target: PortfolioTarget,
    ) -> None:
        account_row = connection.execute(
            select(
                portfolio_account_snapshots.c.snapshot_hash,
                portfolio_account_snapshots.c.cycle_id,
                portfolio_account_snapshots.c.portfolio_id,
                portfolio_account_snapshots.c.payload,
            ).where(
                portfolio_account_snapshots.c.snapshot_id
                == target.account_snapshot_id
            )
        ).one_or_none()
        if account_row is None or (
            account_row.snapshot_hash != target.account_snapshot_hash
            or account_row.cycle_id != target.cycle_id
            or account_row.portfolio_id != target.portfolio_id
            or PortfolioAccountSnapshot.model_validate(account_row.payload).as_of
            != target.as_of
        ):
            raise ValueError("PortfolioTarget 缺少匹配的权威账户快照")
        forecast_ids = target.considered_forecast_ids
        if not forecast_ids:
            return
        rows = connection.execute(
            select(
                forecasts.c.forecast_id,
                forecasts.c.kind,
                forecasts.c.payload,
            ).where(
                forecasts.c.forecast_id.in_(forecast_ids)
            )
        ).all()
        loaded = {
            row.forecast_id: CalibratedForecast.model_validate(row.payload)
            for row in rows
            if row.kind == ForecastKind.CALIBRATED.value
        }
        if set(loaded) != set(forecast_ids):
            raise ValueError("PortfolioTarget 只能引用已持久化 CalibratedForecast")
        required_quote_keys = {
            leg.instrument.key
            for forecast in loaded.values()
            for leg in forecast.target.legs
        }
        if {item.instrument.key for item in target.quotes} != required_quote_keys:
            raise ValueError("PortfolioTarget quotes 必须精确覆盖考虑集 Instruments")
        for sleeve in target.sleeves:
            if any(
                loaded[forecast_id].target != sleeve.forecast_target
                or loaded[forecast_id].forecast_family != sleeve.forecast_family
                for forecast_id in sleeve.forecast_ids
            ):
                raise ValueError("PortfolioTarget Sleeve 与权威 Forecast 身份不一致")


class SqlPortfolioPerformanceStore:
    """Append one causal net-equity interval for every account snapshot after inception."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        end: PortfolioAccountSnapshot,
    ) -> PortfolioPerformanceInterval | None:
        persisted_end = self._account(end.snapshot_id)
        if persisted_end != end:
            raise ValueError("Portfolio Performance end snapshot 不是权威账户事实")
        existing = self.for_end_snapshot(end.snapshot_id)
        if existing is not None:
            return existing
        if end.revision == 0:
            return None
        if not self._is_latest(end):
            raise ValueError("Portfolio Performance 只允许按账户因果顺序追加")
        snapshots = self._unrecorded_snapshots(end)
        intervals = tuple(
            PortfolioPerformanceInterval.between(start, finish)
            for start, finish in pairwise(snapshots)
        )
        if not intervals:
            return None
        try:
            with self._engine.begin() as connection:
                for interval in intervals:
                    self._require_snapshots(connection, interval)
                    connection.execute(
                        insert(portfolio_performance_intervals).values(
                            interval_id=interval.interval_id,
                            portfolio_id=interval.portfolio_id,
                            start_snapshot_id=interval.start_snapshot_id,
                            end_snapshot_id=interval.end_snapshot_id,
                            start_as_of=interval.start_as_of,
                            end_as_of=interval.end_as_of,
                            start_revision=interval.start_revision,
                            end_revision=interval.end_revision,
                            kind=interval.kind.value,
                            net_pnl=interval.net_pnl,
                            return_fraction=interval.return_fraction,
                            interval_hash=content_hash(interval),
                            payload=interval.model_dump(mode="json"),
                        )
                    )
        except IntegrityError:
            existing = self.for_end_snapshot(end.snapshot_id)
            if existing != intervals[-1]:
                raise ValueError(
                    "Portfolio Performance end snapshot 已存在且内容不同"
                ) from None
        return intervals[-1]

    def _account(self, snapshot_id: str) -> PortfolioAccountSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_account_snapshots.c.payload).where(
                    portfolio_account_snapshots.c.snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
        return (
            None if payload is None else PortfolioAccountSnapshot.model_validate(payload)
        )

    def _is_latest(self, end: PortfolioAccountSnapshot) -> bool:
        with self._engine.connect() as connection:
            snapshot_id = connection.execute(
                select(portfolio_account_snapshots.c.snapshot_id)
                .where(
                    portfolio_account_snapshots.c.portfolio_id == end.portfolio_id
                )
                .order_by(
                    portfolio_account_snapshots.c.as_of.desc(),
                    portfolio_account_snapshots.c.revision.desc(),
                    portfolio_account_snapshots.c.snapshot_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return snapshot_id == end.snapshot_id

    def for_end_snapshot(
        self,
        snapshot_id: str,
    ) -> PortfolioPerformanceInterval | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_performance_intervals.c.payload).where(
                    portfolio_performance_intervals.c.end_snapshot_id == snapshot_id
                )
            ).scalar_one_or_none()
        return (
            None
            if payload is None
            else PortfolioPerformanceInterval.model_validate(payload)
        )

    def latest(
        self,
        *,
        portfolio_id: str,
    ) -> PortfolioPerformanceInterval | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_performance_intervals.c.payload)
                .where(portfolio_performance_intervals.c.portfolio_id == portfolio_id)
                .order_by(
                    portfolio_performance_intervals.c.end_as_of.desc(),
                    portfolio_performance_intervals.c.end_revision.desc(),
                    portfolio_performance_intervals.c.interval_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return (
            None
            if payload is None
            else PortfolioPerformanceInterval.model_validate(payload)
        )

    def count(self, *, portfolio_id: str) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.scalar(
                    select(func.count())
                    .select_from(portfolio_performance_intervals)
                    .where(
                        portfolio_performance_intervals.c.portfolio_id
                        == portfolio_id
                    )
                )
                or 0
            )

    def _unrecorded_snapshots(
        self,
        end: PortfolioAccountSnapshot,
    ) -> tuple[PortfolioAccountSnapshot, ...]:
        with self._engine.connect() as connection:
            boundary_payload = connection.execute(
                select(portfolio_performance_intervals.c.payload)
                .where(
                    portfolio_performance_intervals.c.portfolio_id == end.portfolio_id
                )
                .order_by(
                    portfolio_performance_intervals.c.end_revision.desc(),
                    portfolio_performance_intervals.c.interval_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            boundary = (
                None
                if boundary_payload is None
                else PortfolioPerformanceInterval.model_validate(boundary_payload)
            )
            start_revision = boundary.end_revision if boundary is not None else 0
            payloads = connection.execute(
                select(portfolio_account_snapshots.c.payload)
                .where(
                    portfolio_account_snapshots.c.portfolio_id == end.portfolio_id,
                    portfolio_account_snapshots.c.revision >= start_revision,
                    portfolio_account_snapshots.c.revision <= end.revision,
                )
                .order_by(portfolio_account_snapshots.c.revision)
            ).scalars()
            snapshots = tuple(
                PortfolioAccountSnapshot.model_validate(payload) for payload in payloads
            )
        if boundary is not None and (
            not snapshots or snapshots[0].snapshot_id != boundary.end_snapshot_id
        ):
            raise ValueError("Portfolio Performance 账本边界与账户历史不一致")
        if boundary is None and snapshots and snapshots[0].revision != 0:
            raise ValueError("Portfolio Performance 缺少 revision 0 账户基线")
        if not snapshots or snapshots[-1] != end:
            raise ValueError("Portfolio Performance 无法定位当前权威账户")
        return snapshots

    @staticmethod
    def _require_snapshots(
        connection: Connection,
        interval: PortfolioPerformanceInterval,
    ) -> None:
        rows = connection.execute(
            select(
                portfolio_account_snapshots.c.snapshot_id,
                portfolio_account_snapshots.c.portfolio_id,
            ).where(
                portfolio_account_snapshots.c.snapshot_id.in_(
                    (interval.start_snapshot_id, interval.end_snapshot_id)
                )
            )
        ).all()
        if {row.snapshot_id for row in rows} != {
            interval.start_snapshot_id,
            interval.end_snapshot_id,
        } or any(row.portfolio_id != interval.portfolio_id for row in rows):
            raise ValueError("Portfolio Performance 缺少匹配的权威账户快照")
