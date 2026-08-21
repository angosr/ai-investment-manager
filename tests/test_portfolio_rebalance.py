from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select

from investment_manager.portfolio.rebalance import PortfolioRebalancePeriod
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.portfolio.tables import portfolio_rebalance_periods
from investment_manager.schema import create_schema


def test_rebalance_period_is_first_writer_wins_and_survives_restart() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    first = PortfolioRebalancePeriod.create(
        portfolio_id="primary",
        policy_version="monthly-first-open-v2",
        period_start=start,
        period_end=datetime(2026, 10, 1, tzinfo=UTC),
        entry_window_end=start + timedelta(minutes=30),
        decision_at=start + timedelta(minutes=31),
        candidate_forecast_id=None,
    )
    competing = first.model_copy(
        update={"decision_at": start + timedelta(hours=1)}
    )

    assert SqlPortfolioStore(engine).claim_rebalance_period(first) == first
    assert SqlPortfolioStore(engine).claim_rebalance_period(competing) == first
    assert (
        SqlPortfolioStore(engine).rebalance_period(
            portfolio_id="primary",
            policy_version="monthly-first-open-v2",
            period_start=start,
        )
        == first
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(portfolio_rebalance_periods)
            )
            == 1
        )
