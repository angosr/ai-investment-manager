from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from itertools import pairwise
from typing import Protocol

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.models import BaseForecast, CalibratedForecast, ForecastKind
from investment_manager.forecast.tables import forecasts
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.platform.locking import advisory_xact_lock
from investment_manager.portfolio.models import (
    CapitalCycleRecord,
    PortfolioAccountSnapshot,
    PortfolioEdgeBasis,
    PortfolioPerformanceInterval,
    PortfolioTarget,
)
from investment_manager.portfolio.tables import (
    capital_cycle_records,
    portfolio_account_snapshots,
    portfolio_performance_intervals,
    portfolio_target_forecasts,
    portfolio_targets,
)


class PortfolioStore(Protocol):
    def record_account(self, account: PortfolioAccountSnapshot) -> bool: ...

    def record_target(self, target: PortfolioTarget) -> bool: ...

    def account_projection_lock(
        self,
        *,
        portfolio_id: str,
    ) -> AbstractContextManager[None]: ...

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

class SqlPortfolioStore:
    """Immutable account/target ledger; retries must reproduce exact facts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def account_projection_lock(
        self,
        *,
        portfolio_id: str,
    ) -> Iterator[None]:
        """Serialize latest-account projection across processes on PostgreSQL."""

        with self._engine.begin() as connection:
            advisory_xact_lock(
                connection,
                "portfolio_account_projection",
                portfolio_id,
            )
            yield

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
            row.forecast_id: (
                BaseForecast.model_validate(row.payload)
                if row.kind == ForecastKind.BASE.value
                else CalibratedForecast.model_validate(row.payload)
            )
            for row in rows
        }
        if set(loaded) != set(forecast_ids):
            raise ValueError("PortfolioTarget 引用了不存在的 Forecast")
        required_quote_keys = {
            leg.instrument.key
            for forecast in loaded.values()
            for leg in forecast.target.legs
        }
        if {item.instrument.key for item in target.quotes} != required_quote_keys:
            raise ValueError("PortfolioTarget quotes 必须精确覆盖考虑集 Instruments")
        for sleeve in target.sleeves:
            if any(
                (
                    isinstance(loaded[forecast_id], BaseForecast)
                    != (sleeve.edge_basis == PortfolioEdgeBasis.MOCK_HYPOTHESIS)
                )
                or loaded[forecast_id].target != sleeve.forecast_target
                or loaded[forecast_id].forecast_family != sleeve.forecast_family
                for forecast_id in sleeve.forecast_ids
            ):
                raise ValueError("PortfolioTarget Sleeve 与 Forecast edge basis/身份不一致")


class SqlCapitalCycleStore:
    """Persist a causal capital receipt after validating all domain references."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, record: CapitalCycleRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                account = connection.execute(
                    select(portfolio_account_snapshots.c.payload).where(
                        portfolio_account_snapshots.c.snapshot_id
                        == record.account_snapshot_id
                    )
                ).scalar_one_or_none()
                if account is None or PortfolioAccountSnapshot.model_validate(
                    account
                ).portfolio_id != record.portfolio_id:
                    raise ValueError("CapitalCycleRecord 缺少匹配账户快照")
                if record.target_id is not None:
                    target = connection.execute(
                        select(portfolio_targets.c.payload).where(
                            portfolio_targets.c.target_id == record.target_id
                        )
                    ).scalar_one_or_none()
                    loaded_target = (
                        None if target is None else PortfolioTarget.model_validate(target)
                    )
                    if loaded_target is None or (
                        loaded_target.portfolio_id != record.portfolio_id
                        or loaded_target.cycle_id != record.decision_cycle_id
                    ):
                        raise ValueError("CapitalCycleRecord 缺少匹配 PortfolioTarget")
                if record.forecast_ids:
                    present = set(
                        connection.execute(
                            select(forecasts.c.forecast_id).where(
                                forecasts.c.forecast_id.in_(record.forecast_ids)
                            )
                        ).scalars()
                    )
                    if present != set(record.forecast_ids):
                        raise ValueError("CapitalCycleRecord 引用了不存在的 Forecast")
                connection.execute(
                    insert(capital_cycle_records).values(
                        record_id=record.record_id,
                        portfolio_id=record.portfolio_id,
                        pipeline_id=record.pipeline_id,
                        cause_id=record.cause_id,
                        evaluated_at=record.evaluated_at,
                        decision_cycle_id=record.decision_cycle_id,
                        account_snapshot_id=record.account_snapshot_id,
                        target_id=record.target_id,
                        outcome=record.outcome.value,
                        payload=record.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.get(record.record_id)
            if existing != record:
                raise ValueError(
                    "CapitalCycleRecord evaluation 已存在且内容不同"
                ) from None
            return False

    def get(self, record_id: str) -> CapitalCycleRecord | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(capital_cycle_records.c.payload).where(
                    capital_cycle_records.c.record_id == record_id
                )
            ).scalar_one_or_none()
        return None if payload is None else CapitalCycleRecord.model_validate(payload)


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
