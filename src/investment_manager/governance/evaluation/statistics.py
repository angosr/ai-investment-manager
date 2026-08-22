"""Deterministic statistics shared by preregistered forward evaluators."""

from decimal import Decimal


def conservative_newey_west_lower_bound(
    values: tuple[Decimal, ...],
    *,
    z: Decimal,
    lag: int,
) -> Decimal:
    """Mean lower bound that never rewards negative serial correlation."""

    if len(values) < 2:
        raise ValueError("保守下界至少需要两个独立时间窗口")
    if lag < 1 or lag >= len(values):
        raise ValueError("Newey-West lag 必须小于时间窗口数")
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    residuals = tuple(item - mean for item in values)
    gamma_zero = sum((item**2 for item in residuals), Decimal("0")) / count
    long_run_variance = gamma_zero
    for offset in range(1, lag + 1):
        covariance = (
            sum(
                (
                    residuals[index] * residuals[index - offset]
                    for index in range(offset, len(residuals))
                ),
                Decimal("0"),
            )
            / count
        )
        weight = Decimal(1) - Decimal(offset) / Decimal(lag + 1)
        long_run_variance += Decimal(2) * weight * covariance
    conservative_variance = max(gamma_zero, long_run_variance, Decimal("0"))
    return mean - z * (conservative_variance / count).sqrt()
