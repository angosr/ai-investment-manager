"""Point-in-time executable mapping evidence for one Reference product candidate."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from sqlalchemy import Engine, func, select

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
    MarketReferencePrice,
)
from investment_manager.market.tables import market_quotes, market_reference_prices
from investment_manager.platform.artifacts import write_json_artifact

_BPS = Decimal("10000")


class ReferenceProductMappingObservation(FrozenModel):
    quote_id: str = Field(min_length=1)
    reference_price_id: str = Field(min_length=1)
    quote_observed_at: datetime
    reference_exchange_time: datetime
    reference_observed_at: datetime
    bid: PositiveDecimal
    bid_quantity: Money
    ask: PositiveDecimal
    ask_quantity: Money
    reference_price: PositiveDecimal
    reference_age_ms: int = Field(ge=0)
    bid_premium_bps: Decimal
    ask_premium_bps: Decimal
    mid_premium_bps: Decimal
    spread_bps: Decimal = Field(ge=0)
    bid_top_notional: Money
    ask_top_notional: Money

    _utc_quote = field_validator("quote_observed_at")(require_utc)
    _utc_reference_exchange = field_validator("reference_exchange_time")(require_utc)
    _utc_reference_observed = field_validator("reference_observed_at")(require_utc)

    @model_validator(mode="after")
    def timing_and_derived_values_match(self):
        if not (
            self.reference_exchange_time
            <= self.reference_observed_at
            <= self.quote_observed_at
        ):
            raise ValueError("产品映射事实时间或可见性非法")
        if self.ask < self.bid:
            raise ValueError("产品映射 ask 不能低于 bid")
        expected_age = int(
            (self.quote_observed_at - self.reference_exchange_time).total_seconds()
            * 1000
        )
        if self.reference_age_ms != expected_age:
            raise ValueError("产品映射 reference age 与时间不一致")
        mid = (self.bid + self.ask) / Decimal("2")
        expected = (
            (self.bid / self.reference_price - 1) * _BPS,
            (self.ask / self.reference_price - 1) * _BPS,
            (mid / self.reference_price - 1) * _BPS,
            ((self.ask - self.bid) / self.reference_price) * _BPS,
            self.bid * self.bid_quantity,
            self.ask * self.ask_quantity,
        )
        observed = (
            self.bid_premium_bps,
            self.ask_premium_bps,
            self.mid_premium_bps,
            self.spread_bps,
            self.bid_top_notional,
            self.ask_top_notional,
        )
        if observed != expected:
            raise ValueError("产品映射衍生指标与原始报价不一致")
        return self


class ReferenceProductMappingArtifact(FrozenModel):
    schema_version: str = "reference-product-mapping-v1"
    artifact_id: str
    captured_at: datetime
    instrument: InstrumentId
    reference_contract: str = Field(min_length=1)
    reference_calculation_type: str = Field(min_length=1)
    reference_external_calculation_id: int = Field(gt=0)
    requested_start: datetime
    requested_end: datetime
    sampling_interval_seconds: int = Field(gt=0)
    maximum_reference_age_ms: int = Field(gt=0)
    source_quote_count: int = Field(gt=0)
    source_reference_count: int = Field(gt=0)
    sampled_quote_count: int = Field(gt=0)
    matched_quote_count: int = Field(gt=1)
    unmatched_quote_count: int = Field(ge=0)
    first_quote_observed_at: datetime
    last_quote_observed_at: datetime
    matched_fraction: Decimal = Field(gt=0, le=1)
    mean_absolute_mid_premium_bps: Decimal = Field(ge=0)
    maximum_absolute_mid_premium_bps: Decimal = Field(ge=0)
    mean_spread_bps: Decimal = Field(ge=0)
    maximum_spread_bps: Decimal = Field(ge=0)
    minimum_bid_top_notional: Money
    minimum_ask_top_notional: Money
    maximum_observed_reference_age_ms: int = Field(ge=0)
    observations_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations: tuple[ReferenceProductMappingObservation, ...] = Field(min_length=2)

    _utc_captured = field_validator("captured_at")(require_utc)
    _utc_start = field_validator("requested_start")(require_utc)
    _utc_end = field_validator("requested_end")(require_utc)
    _utc_first = field_validator("first_quote_observed_at")(require_utc)
    _utc_last = field_validator("last_quote_observed_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_summary_match(self):
        if self.instrument.product != InstrumentProduct.SPOT:
            raise ValueError("Reference 产品映射候选必须是 Spot")
        if not (
            self.requested_start
            <= self.first_quote_observed_at
            <= self.last_quote_observed_at
            < self.requested_end
            <= self.captured_at
        ):
            raise ValueError("Reference 产品映射窗口非法")
        if self.matched_quote_count != len(self.observations):
            raise ValueError("Reference 产品映射匹配数量不一致")
        if self.sampled_quote_count != (
            self.matched_quote_count + self.unmatched_quote_count
        ):
            raise ValueError("Reference 产品映射采样覆盖不一致")
        times = tuple(item.quote_observed_at for item in self.observations)
        if times != tuple(sorted(set(times))):
            raise ValueError("Reference 产品映射观测必须唯一递增")
        if times[0] != self.first_quote_observed_at or times[-1] != self.last_quote_observed_at:
            raise ValueError("Reference 产品映射观测边界不一致")
        if any(
            item.reference_age_ms > self.maximum_reference_age_ms
            for item in self.observations
        ):
            raise ValueError("Reference 产品映射包含超龄参考价格")
        if self.observations_hash != content_hash(self.observations):
            raise ValueError("Reference 产品映射观测哈希不一致")
        expected_summary = _summary(
            self.observations,
            sampled_quote_count=self.sampled_quote_count,
        )
        for key, value in expected_summary.items():
            if getattr(self, key) != value:
                raise ValueError(f"Reference 产品映射摘要不一致: {key}")
        expected_id = stable_id(
            "reference_product_mapping",
            content_hash(self.model_dump(mode="json", exclude={"artifact_id"})),
        )
        if self.artifact_id != expected_id:
            raise ValueError("Reference 产品映射制品身份不一致")
        return self


def freeze_reference_product_mapping(
    engine: Engine,
    *,
    instrument: InstrumentId,
    start: datetime,
    end: datetime,
    sampling_interval_seconds: int,
    maximum_reference_age_ms: int,
    reference_contract: str,
    reference_calculation_type: str,
    reference_external_calculation_id: int,
    captured_at: datetime,
) -> ReferenceProductMappingArtifact:
    start = require_utc(start)
    end = require_utc(end)
    captured_at = require_utc(captured_at)
    if instrument.product != InstrumentProduct.SPOT:
        raise ValueError("Reference 产品映射只接受 Spot 候选")
    if start >= end or end > captured_at:
        raise ValueError("Reference 产品映射请求窗口非法")
    if sampling_interval_seconds < 1 or maximum_reference_age_ms < 1:
        raise ValueError("Reference 产品映射采样和参考年龄必须为正数")
    reference_start = start - timedelta(milliseconds=maximum_reference_age_ms)

    with engine.connect() as connection:
        source_quote_count = connection.execute(
            select(func.count()).select_from(market_quotes).where(
                market_quotes.c.symbol == instrument.symbol,
                market_quotes.c.observed_at >= start,
                market_quotes.c.observed_at < end,
            )
        ).scalar_one()
        source_reference_count = connection.execute(
            select(func.count()).select_from(market_reference_prices).where(
                market_reference_prices.c.symbol == instrument.symbol,
                market_reference_prices.c.observed_at >= reference_start,
                market_reference_prices.c.observed_at < end,
            )
        ).scalar_one()
    if source_quote_count < 2 or source_reference_count < 2:
        raise ValueError("Reference 产品映射缺少至少两个报价或参考价格")

    sampled_quote_count = 0
    unmatched_quote_count = 0
    observations: list[ReferenceProductMappingObservation] = []
    with (
        engine.connect().execution_options(stream_results=True) as quote_connection,
        engine.connect().execution_options(stream_results=True) as reference_connection,
    ):
        references = _reference_prices(
            reference_connection,
            instrument.symbol,
            reference_start,
            end,
        )
        current_reference: MarketReferencePrice | None = None
        next_reference = next(references, None)
        previous_bucket: int | None = None
        for quote in _quotes(quote_connection, instrument.symbol, start, end):
            bucket = int(quote.observed_at.timestamp()) // sampling_interval_seconds
            if bucket == previous_bucket:
                continue
            previous_bucket = bucket
            sampled_quote_count += 1
            while (
                next_reference is not None
                and next_reference.observed_at <= quote.observed_at
            ):
                current_reference = next_reference
                next_reference = next(references, None)
            if current_reference is None:
                unmatched_quote_count += 1
                continue
            age_ms = int(
                (quote.observed_at - current_reference.exchange_time).total_seconds()
                * 1000
            )
            if age_ms < 0 or age_ms > maximum_reference_age_ms:
                unmatched_quote_count += 1
                continue
            observations.append(_mapping_observation(quote, current_reference, age_ms))
    if len(observations) < 2:
        raise ValueError("Reference 产品映射没有至少两个同步匹配样本")

    frozen = tuple(observations)
    values = {
        "schema_version": "reference-product-mapping-v1",
        "captured_at": captured_at,
        "instrument": instrument,
        "reference_contract": reference_contract,
        "reference_calculation_type": reference_calculation_type,
        "reference_external_calculation_id": reference_external_calculation_id,
        "requested_start": start,
        "requested_end": end,
        "sampling_interval_seconds": sampling_interval_seconds,
        "maximum_reference_age_ms": maximum_reference_age_ms,
        "source_quote_count": source_quote_count,
        "source_reference_count": source_reference_count,
        "sampled_quote_count": sampled_quote_count,
        "matched_quote_count": len(frozen),
        "unmatched_quote_count": unmatched_quote_count,
        "first_quote_observed_at": frozen[0].quote_observed_at,
        "last_quote_observed_at": frozen[-1].quote_observed_at,
        **_summary(frozen, sampled_quote_count=sampled_quote_count),
        "observations_hash": content_hash(frozen),
        "observations": frozen,
    }
    pending = ReferenceProductMappingArtifact.model_construct(
        artifact_id="pending",
        **values,
    )
    return ReferenceProductMappingArtifact(
        artifact_id=stable_id(
            "reference_product_mapping",
            content_hash(pending.model_dump(mode="json", exclude={"artifact_id"})),
        ),
        **values,
    )


def store_reference_product_mapping(
    artifact: ReferenceProductMappingArtifact,
    *,
    root: Path,
) -> Path:
    target = root / f"{artifact.artifact_id}.json"
    if target.exists():
        if ReferenceProductMappingArtifact.model_validate_json(
            target.read_text(encoding="utf-8")
        ) != artifact:
            raise ValueError("同一 Reference 产品映射 ID 的内容不一致")
        return target
    return write_json_artifact(
        root=root,
        target=target,
        prefix=".reference-product-mapping-",
        payload=artifact,
    )


def _quotes(connection, symbol: str, start: datetime, end: datetime) -> Iterator[MarketQuote]:
    rows = connection.execute(
        select(market_quotes.c.payload)
        .where(
            market_quotes.c.symbol == symbol,
            market_quotes.c.observed_at >= start,
            market_quotes.c.observed_at < end,
        )
        .order_by(market_quotes.c.observed_at, market_quotes.c.quote_id)
    ).scalars()
    for payload in rows:
        yield MarketQuote.model_validate(payload)


def _reference_prices(
    connection,
    symbol: str,
    start: datetime,
    end: datetime,
) -> Iterator[MarketReferencePrice]:
    rows = connection.execute(
        select(market_reference_prices.c.payload)
        .where(
            market_reference_prices.c.symbol == symbol,
            market_reference_prices.c.observed_at >= start,
            market_reference_prices.c.observed_at < end,
        )
        .order_by(
            market_reference_prices.c.observed_at,
            market_reference_prices.c.reference_price_id,
        )
    ).scalars()
    for payload in rows:
        yield MarketReferencePrice.model_validate(payload)


def _mapping_observation(
    quote: MarketQuote,
    reference: MarketReferencePrice,
    age_ms: int,
) -> ReferenceProductMappingObservation:
    mid = (quote.bid + quote.ask) / Decimal("2")
    return ReferenceProductMappingObservation(
        quote_id=quote.quote_id,
        reference_price_id=reference.reference_price_id,
        quote_observed_at=quote.observed_at,
        reference_exchange_time=reference.exchange_time,
        reference_observed_at=reference.observed_at,
        bid=quote.bid,
        bid_quantity=quote.bid_quantity,
        ask=quote.ask,
        ask_quantity=quote.ask_quantity,
        reference_price=reference.price,
        reference_age_ms=age_ms,
        bid_premium_bps=(quote.bid / reference.price - 1) * _BPS,
        ask_premium_bps=(quote.ask / reference.price - 1) * _BPS,
        mid_premium_bps=(mid / reference.price - 1) * _BPS,
        spread_bps=((quote.ask - quote.bid) / reference.price) * _BPS,
        bid_top_notional=quote.bid * quote.bid_quantity,
        ask_top_notional=quote.ask * quote.ask_quantity,
    )


def _summary(
    observations: tuple[ReferenceProductMappingObservation, ...],
    *,
    sampled_quote_count: int,
) -> dict[str, Decimal | int]:
    absolute_premiums = tuple(abs(item.mid_premium_bps) for item in observations)
    spreads = tuple(item.spread_bps for item in observations)
    return {
        "matched_fraction": Decimal(len(observations)) / Decimal(sampled_quote_count),
        "mean_absolute_mid_premium_bps": sum(
            absolute_premiums, Decimal("0")
        )
        / Decimal(len(absolute_premiums)),
        "maximum_absolute_mid_premium_bps": max(absolute_premiums),
        "mean_spread_bps": sum(spreads, Decimal("0")) / Decimal(len(spreads)),
        "maximum_spread_bps": max(spreads),
        "minimum_bid_top_notional": min(
            item.bid_top_notional for item in observations
        ),
        "minimum_ask_top_notional": min(
            item.ask_top_notional for item in observations
        ),
        "maximum_observed_reference_age_ms": max(
            item.reference_age_ms for item in observations
        ),
    }


__all__ = [
    "ReferenceProductMappingArtifact",
    "ReferenceProductMappingObservation",
    "freeze_reference_product_mapping",
    "store_reference_product_mapping",
]
