from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_manager.domain import MarketSnapshot, floor_to_step


def test_floor_to_step_is_exact_and_rejects_invalid_step() -> None:
    assert floor_to_step(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert floor_to_step(Decimal("1.23"), Decimal("0.01")) == Decimal("1.23")
    assert floor_to_step(Decimal("0.009"), Decimal("0.01")) == Decimal("0.00")

    with pytest.raises(ValueError, match="步长必须大于零"):
        floor_to_step(Decimal("1"), Decimal("0"))


def test_market_snapshot_rejects_future_observation(replay_input) -> None:
    payload = replay_input.market.model_dump(mode="json")
    payload["bars"][-1]["observed_at"] = "2026-08-18T12:00:01Z"

    try:
        MarketSnapshot.model_validate(payload)
    except ValidationError as exc:
        assert "as_of 之后" in str(exc)
    else:
        raise AssertionError("未来数据必须被拒绝")


def test_models_reject_unknown_fields(replay_input) -> None:
    payload = replay_input.market.model_dump(mode="json")
    payload["hidden_signal"] = "BUY"

    try:
        MarketSnapshot.model_validate(payload)
    except ValidationError as exc:
        assert "extra_forbidden" in str(exc)
    else:
        raise AssertionError("未声明字段必须被拒绝")
