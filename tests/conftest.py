from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_core.config import AppConfig, load_config
from quant_core.cycle import CycleInput

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_config() -> AppConfig:
    return load_config(ROOT / "config" / "quant-core.yaml")


@pytest.fixture
def replay_input() -> CycleInput:
    raw = json.loads((ROOT / "fixtures" / "replay" / "btc_uptrend.json").read_text())
    return CycleInput.model_validate(raw)
