from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.models import CalibratedForecast, ForecastKind
from investment_manager.forecast.tables import forecasts
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    PortfolioTarget,
)
from investment_manager.portfolio.tables import (
    portfolio_account_snapshots,
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
