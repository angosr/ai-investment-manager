"""Stable value constraints shared by every investment domain."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Money = Annotated[Decimal, Field(ge=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
UnitInterval = Annotated[Decimal, Field(ge=0, le=1)]


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to an exact venue step using one system-wide rule."""
    if step <= 0:
        raise ValueError("步长必须大于零")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class FrozenModel(BaseModel):
    """Strict immutable base for persisted and serialized domain facts."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
