from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import create_engine

from investment_manager.execution.cash.models import (
    CashYieldProductObservation,
    build_cash_yield_product_observation,
)
from investment_manager.execution.cash.policy import CashYieldEvidencePolicy
from investment_manager.execution.cash.repository import SqlCashYieldObservationStore
from investment_manager.execution.cash.service import CashYieldEvidenceService
from investment_manager.execution.cash.source import (
    BinanceReadCredentials,
    BinanceSimpleEarnReadSource,
)
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 28, 22, 30, tzinfo=UTC)


class _Transport:
    def request(self, method, url, *, headers, timeout_seconds):
        assert method == "GET"
        assert timeout_seconds == 5
        parsed = urlparse(url)
        if parsed.path == "/api/v3/time":
            return 200, {"serverTime": int(NOW.timestamp() * 1000)}
        assert headers == {"X-MBX-APIKEY": "key"}
        assert "signature=" in parsed.query
        if parsed.path.endswith("/flexible/list"):
            return 200, {
                "rows": [
                    {
                        "productId": "USDT001",
                        "asset": "USDT",
                        "latestAnnualPercentageRate": "0.028",
                        "canPurchase": True,
                        "canRedeem": True,
                        "isSoldOut": False,
                        "minPurchaseAmount": "0.01",
                    }
                ]
            }
        if parsed.path.endswith("/personalLeftQuota"):
            return 200, {"leftPersonalQuota": "20000"}
        if parsed.path.endswith("/subscriptionPreview"):
            return 200, {
                "estDailyRealTimeRewards": "0.76",
                "estDailyBonusRewards": "0",
                "estDailyAirdropRewards": "0",
                "rewardAsset": "USDT",
            }
        raise AssertionError(parsed.path)


def _policy() -> CashYieldEvidencePolicy:
    return CashYieldEvidencePolicy(
        version="cash-evidence-v1",
        enabled=True,
        refresh_seconds=3600,
        request_timeout_seconds=5,
    )


def _observation(*, observed_at: datetime = NOW) -> CashYieldProductObservation:
    return build_cash_yield_product_observation(
        policy_version="cash-evidence-v1",
        product_id="USDT001",
        asset="USDT",
        observed_at=observed_at,
        available_at=observed_at,
        annual_rate=Decimal("0.028"),
        minimum_purchase_amount=Decimal("0.01"),
        left_personal_quota=Decimal("20000"),
        can_purchase=True,
        can_redeem=True,
        sold_out=False,
        preview_amount=Decimal("10000"),
        preview_daily_reward=Decimal("0.76"),
        reward_asset="USDT",
        source_refs=("https://api.binance.com/read-only",),
    )


def test_binance_cash_yield_source_is_get_only_and_builds_one_complete_fact() -> None:
    source = BinanceSimpleEarnReadSource(
        _policy(),
        BinanceReadCredentials(api_key="key", api_secret="secret"),
        transport=_Transport(),
        clock=lambda: NOW,
    )

    observation = source.fetch(preview_amount=Decimal("10000"))

    assert observation.annual_rate == Decimal("0.028")
    assert observation.preview_daily_reward == Decimal("0.76")
    assert observation.left_personal_quota == Decimal("20000")
    assert observation.available_at == NOW


def test_cash_yield_service_persists_once_and_respects_source_frequency() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    store = SqlCashYieldObservationStore(engine)

    class Source:
        calls = 0

        def fetch(self, *, preview_amount):
            self.calls += 1
            return _observation()

    source = Source()
    service = CashYieldEvidenceService(
        policy=_policy(),
        source=source,
        store=store,
        clock=lambda: NOW,
    )

    first = service.observe(preview_amount=Decimal("10000"))
    second = service.observe(preview_amount=Decimal("9000"))

    assert first.recorded and first.refreshed
    assert not second.recorded and not second.refreshed
    assert second.observation == first.observation
    assert source.calls == 1
    assert store.latest(
        product_id="USDT001",
        asset="USDT",
        visible_at=NOW + timedelta(seconds=1),
    ) == first.observation
