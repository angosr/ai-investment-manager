from datetime import UTC, datetime, timedelta, timezone

from quant_core.sql_time import database_utc


def test_database_utc_attaches_utc_to_naive_driver_value() -> None:
    value = datetime(2026, 8, 20, 10, 30)

    assert database_utc(value) == datetime(2026, 8, 20, 10, 30, tzinfo=UTC)


def test_database_utc_normalizes_aware_driver_value() -> None:
    value = datetime(2026, 8, 20, 18, 30, tzinfo=timezone(timedelta(hours=8)))

    assert database_utc(value) == datetime(2026, 8, 20, 10, 30, tzinfo=UTC)
