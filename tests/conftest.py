from __future__ import annotations

import json
from pathlib import Path

import pytest

from investment_manager.execution.models import AccountSnapshot
from investment_manager.information.models import IntelligenceEvent
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import MarketSnapshot
from investment_manager.settings import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


class ReplayInput(FrozenModel):
    """Small point-in-time fixture shared by current domain tests."""

    market: MarketSnapshot
    account: AccountSnapshot
    events: tuple[IntelligenceEvent, ...] = ()


@pytest.fixture
def base_app_config() -> AppConfig:
    return load_config(ROOT / "config" / "investment-manager.yaml")


@pytest.fixture
def app_config(base_app_config: AppConfig) -> AppConfig:
    return base_app_config


@pytest.fixture
def replay_input() -> ReplayInput:
    raw = json.loads((ROOT / "fixtures" / "replay" / "btc_uptrend.json").read_text())
    return ReplayInput.model_validate(
        {name: raw[name] for name in ("market", "account", "events")}
    )
