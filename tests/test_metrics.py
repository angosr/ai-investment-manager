from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quant_core.metrics import METRIC_DEFINITIONS, observation


def test_metric_registry_is_the_only_way_to_create_observations() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)

    metric = observation("market_data_age_seconds", 181, cycle_id="cycle-1", observed_at=now)

    assert metric.metric_version == "metrics-v3"
    assert METRIC_DEFINITIONS["market_data_age_seconds"].domain == "runtime"
    with pytest.raises(ValueError, match="未注册"):
        observation("dashboard_private_formula", 1, cycle_id="cycle-1", observed_at=now)
