from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from investment_manager.config import AppConfig, load_config
from investment_manager.cycle import CycleInput
from investment_manager.domain import EdgeCalibration, Side
from investment_manager.kernel.identity import content_hash

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def base_app_config() -> AppConfig:
    return load_config(ROOT / "config" / "investment-manager.yaml")


@pytest.fixture
def app_config(base_app_config: AppConfig) -> AppConfig:
    """可走完整成交链路的测试配置。

    生产基线没有发布校准制品，理应不交易。多数领域测试需要继续覆盖风控、执行和
    持仓闭环，因此只在测试夹具中注入一个绑定固定 Producer 的校准制品。
    """

    calibration_payload = {
        "calibration_id": "test-price-trend-calibration-v1",
        "producer_id": base_app_config.strategy.strategy_id,
        "producer_version": base_app_config.strategy.version,
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "horizon_minutes": base_app_config.strategy.horizon_minutes,
        "expected_gross_bps": Decimal("45"),
        "conservative_gross_bps": Decimal("40"),
        "sample_size": 120,
        "non_overlapping_sample_size": 100,
        "training_start": datetime(2025, 1, 1, tzinfo=UTC),
        "training_end": datetime(2025, 12, 31, tzinfo=UTC),
        "published_at": datetime(2026, 1, 1, tzinfo=UTC),
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "valid_until": datetime(2027, 1, 1, tzinfo=UTC),
        "evaluation_version": "test-candidate-evaluation-v1",
        "source_calibration_ref": f"uncalibrated:{base_app_config.strategy.version}",
        "source_execution_policy_version": base_app_config.execution.version,
        "source_frequency_policy_version": base_app_config.frequency.version,
        "method_version": base_app_config.calibration.method_version,
        "dataset_hash": "a" * 64,
    }
    calibration = EdgeCalibration(
        **calibration_payload,
        artifact_hash=content_hash(calibration_payload),
    )
    return base_app_config.model_copy(
        update={
            "calibration": base_app_config.calibration.model_copy(
                update={"artifacts": (calibration,)}
            ),
        }
    )


@pytest.fixture
def replay_input() -> CycleInput:
    raw = json.loads((ROOT / "fixtures" / "replay" / "btc_uptrend.json").read_text())
    return CycleInput.model_validate(raw)
