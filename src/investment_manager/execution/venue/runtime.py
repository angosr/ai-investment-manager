"""System-level execution venue selection.

Deployment mode belongs to composition. Capital decisions receive only the
neutral venue port and account seed selected here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.engine import Engine

from investment_manager.execution.venue.product import ProductOrderVenue
from investment_manager.execution.venue.product_mock import SqlMockProductVenue
from investment_manager.governance.policy import DeploymentStage
from investment_manager.settings import AppConfig


@dataclass(frozen=True, slots=True)
class ProductExecutionRuntime:
    venue: ProductOrderVenue
    initial_cash: Decimal


def assemble_product_execution_runtime(
    config: AppConfig,
    engine: Engine,
) -> ProductExecutionRuntime:
    """Select the authorized venue adapter without leaking stage into capital."""

    if not config.capital.enabled:
        raise ValueError("Capital 未启用，不能装配产品执行环境")
    if config.deployment.stage != DeploymentStage.SHADOW:
        raise ValueError("当前资本执行适配器只支持 SHADOW")
    return ProductExecutionRuntime(
        venue=SqlMockProductVenue(
            engine,
            fee_bps_by_instrument={
                item.instrument.key: item.fee_bps
                for item in config.capital.execution_specs
            },
        ),
        initial_cash=config.shadow.initial_quote_balance,
    )
