from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from investment_manager.execution.cash.models import (
    CashYieldProductObservation,
    build_cash_yield_product_observation,
)
from investment_manager.execution.cash.policy import CashYieldEvidencePolicy
from investment_manager.execution.venue.binance_client import (
    BinanceApiError,
    BinanceTransport,
    HttpxBinanceTransport,
)

_LIST_PATH = "/sapi/v1/simple-earn/flexible/list"
_QUOTA_PATH = "/sapi/v1/simple-earn/flexible/personalLeftQuota"
_PREVIEW_PATH = "/sapi/v1/simple-earn/flexible/subscriptionPreview"


@dataclass(frozen=True, slots=True)
class BinanceReadCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        policy: CashYieldEvidencePolicy,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> BinanceReadCredentials:
        values = environment if environment is not None else os.environ
        prefix = policy.credential_environment_prefix
        key = values.get(f"{prefix}_API_KEY", "").strip()
        secret = values.get(f"{prefix}_API_SECRET", "").strip()
        if not key or not secret:
            raise ValueError("Binance 现金收益只读凭证未配置")
        return cls(api_key=key, api_secret=secret)


class BinanceSimpleEarnReadSource:
    """A GET-only source; it deliberately exposes no subscribe or redeem method."""

    def __init__(
        self,
        policy: CashYieldEvidencePolicy,
        credentials: BinanceReadCredentials,
        *,
        transport: BinanceTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._credentials = credentials
        self._transport = transport or HttpxBinanceTransport()
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch(self, *, preview_amount: Decimal) -> CashYieldProductObservation:
        if preview_amount <= 0:
            raise ValueError("现金收益预览金额必须为正数")
        products = self._signed_get(
            _LIST_PATH,
            {"asset": self._policy.asset, "current": 1, "size": 100},
        )
        rows = products.get("rows") if isinstance(products, dict) else None
        matches = tuple(
            item
            for item in rows or ()
            if isinstance(item, dict)
            and item.get("productId") == self._policy.product_id
            and item.get("asset") == self._policy.asset
        )
        if len(matches) != 1:
            raise ValueError("Binance 未返回唯一现金收益产品")
        product = matches[0]
        quota = self._signed_get(_QUOTA_PATH, {"productId": self._policy.product_id})
        preview = self._signed_get(
            _PREVIEW_PATH,
            {"productId": self._policy.product_id, "amount": str(preview_amount)},
        )
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("现金收益产品观察时钟必须包含时区")
        observed_at = observed_at.astimezone(UTC)
        try:
            annual_rate = Decimal(str(product["latestAnnualPercentageRate"]))
            minimum = Decimal(str(product["minPurchaseAmount"]))
            left_quota = Decimal(str(quota["leftPersonalQuota"]))
            daily_reward = sum(
                (
                    Decimal(str(preview.get(name, "0")))
                    for name in (
                        "estDailyRealTimeRewards",
                        "estDailyBonusRewards",
                        "estDailyAirdropRewards",
                    )
                ),
                Decimal("0"),
            )
        except (InvalidOperation, KeyError, TypeError) as exc:
            raise ValueError("Binance 现金收益数值响应非法") from exc
        return build_cash_yield_product_observation(
            policy_version=self._policy.version,
            product_id=self._policy.product_id,
            asset=self._policy.asset,
            observed_at=observed_at,
            available_at=observed_at,
            annual_rate=annual_rate,
            minimum_purchase_amount=minimum,
            left_personal_quota=left_quota,
            can_purchase=bool(product.get("canPurchase")),
            can_redeem=bool(product.get("canRedeem")),
            sold_out=bool(product.get("isSoldOut")),
            preview_amount=preview_amount,
            preview_daily_reward=daily_reward,
            reward_asset=str(preview.get("rewardAsset", "")),
            source_refs=tuple(
                sorted(
                    f"{self._policy.rest_base_url}{path}"
                    for path in (_LIST_PATH, _PREVIEW_PATH, _QUOTA_PATH)
                )
            ),
        )

    def _server_time_ms(self) -> int:
        status, payload = self._transport.request(
            "GET",
            f"{self._policy.rest_base_url}/api/v3/time",
            headers={},
            timeout_seconds=self._policy.request_timeout_seconds,
        )
        if status != 200 or not isinstance(payload, dict) or "serverTime" not in payload:
            raise BinanceApiError(status, _error_code(payload), "server time unavailable")
        return int(payload["serverTime"])

    def _signed_get(self, path: str, parameters: Mapping[str, Any]) -> Any:
        values = dict(parameters)
        values["recvWindow"] = self._policy.recv_window_ms
        values["timestamp"] = self._server_time_ms()
        query = urlencode(values)
        signature = hmac.new(
            self._credentials.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        status, payload = self._transport.request(
            "GET",
            f"{self._policy.rest_base_url}{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": self._credentials.api_key},
            timeout_seconds=self._policy.request_timeout_seconds,
        )
        if status != 200 or not isinstance(payload, dict):
            raise BinanceApiError(status, _error_code(payload), "read-only probe failed")
        return payload


def _error_code(payload: object) -> int | None:
    value = payload.get("code") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None
