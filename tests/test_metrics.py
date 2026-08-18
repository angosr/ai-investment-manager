from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quant_core.metrics import (
    METRIC_DEFINITIONS,
    AlertRule,
    Comparator,
    evaluate_alert,
    observation,
)


def test_metric_registry_is_the_only_way_to_create_observations() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)

    metric = observation("market_data_age_seconds", 181, cycle_id="cycle-1", observed_at=now)

    assert metric.metric_version == "metrics-v2"
    assert METRIC_DEFINITIONS["market_data_age_seconds"].response == "halt_new_risk_when_stale"
    with pytest.raises(ValueError, match="未注册"):
        observation("dashboard_private_formula", 1, cycle_id="cycle-1", observed_at=now)


def test_alert_rule_references_registered_metric_and_has_explicit_action() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    metric = observation("market_data_age_seconds", 181, cycle_id="cycle-1", observed_at=now)
    rule = AlertRule(
        rule_id="market-stale-v1",
        metric_name="market_data_age_seconds",
        comparator=Comparator.GT,
        threshold=Decimal("180"),
        action="HALT_NEW_RISK",
    )

    alert = evaluate_alert(rule, metric)

    assert alert is not None
    assert alert.action == "HALT_NEW_RISK"
