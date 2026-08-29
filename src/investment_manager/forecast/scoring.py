"""Source-independent proper scores for categorical Forecast outcomes."""

from __future__ import annotations

from decimal import Decimal


def multiclass_brier_score(
    probabilities: tuple[tuple[str, Decimal], ...],
    realized: str,
) -> Decimal:
    if realized not in {bucket_id for bucket_id, _probability in probabilities}:
        raise ValueError("Brier 真实 bucket 不属于预测分布")
    return sum(
        (
            (probability - (Decimal("1") if bucket_id == realized else Decimal("0"))) ** 2
            for bucket_id, probability in probabilities
        ),
        Decimal("0"),
    )


def ordinal_ranked_probability_score(
    probabilities: tuple[tuple[str, Decimal], ...],
    realized: str,
) -> Decimal:
    """Normalized ranked probability score for an ordered outcome contract."""

    bucket_ids = tuple(bucket_id for bucket_id, _probability in probabilities)
    if realized not in bucket_ids:
        raise ValueError("有序概率真实 bucket 不属于预测分布")
    if len(bucket_ids) < 2:
        raise ValueError("有序概率评分至少需要两个 bucket")
    realized_index = bucket_ids.index(realized)
    cumulative = Decimal("0")
    score = Decimal("0")
    for index, (_bucket_id, probability) in enumerate(probabilities[:-1]):
        cumulative += probability
        observed_cumulative = Decimal("1") if realized_index <= index else Decimal("0")
        score += (cumulative - observed_cumulative) ** 2
    return score / Decimal(len(bucket_ids) - 1)
