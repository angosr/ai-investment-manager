from __future__ import annotations

from pydantic import ValidationError

from quant_core.domain import MarketSnapshot


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
