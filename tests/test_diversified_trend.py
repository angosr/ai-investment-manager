from datetime import date

import pytest

from investment_manager.research.diversified_trend import (
    MonthlyTrendInput,
    simulate_monthly_portfolios,
)


def _row(
    month: int,
    *,
    btc: float,
    paxg: float,
    equity: float,
    cash: float,
    directions: tuple[int, int, int],
) -> MonthlyTrendInput:
    return MonthlyTrendInput(
        month=date(2026, month, 1),
        returns={"BTC": btc, "PAXG": paxg, "US_EQUITY": equity, "CASH": cash},
        directions=dict(zip(("BTC", "PAXG", "US_EQUITY"), directions, strict=True)),
        cpi_level=100 + month,
    )


def test_diversified_trend_charges_only_signed_risk_target_changes() -> None:
    outcomes = simulate_monthly_portfolios(
        (
            _row(1, btc=0.06, paxg=0.03, equity=0.015, cash=0.001, directions=(1, 1, 1)),
            _row(2, btc=-0.03, paxg=0.01, equity=0.02, cash=0.001, directions=(-1, 1, 1)),
        ),
        friction_bps=10,
        stress_friction_bps=20,
    )

    assert outcomes[0].candidate_turnover == pytest.approx(0.5)
    assert outcomes[1].candidate_turnover == pytest.approx(1 / 3)
    assert outcomes[0].candidate_return == pytest.approx(
        0.5 * 0.001 + (0.06 + 0.03 + 0.015) / 6 - 0.5 * 0.001
    )
    assert outcomes[1].candidate_return == pytest.approx(
        0.5 * 0.001 + (0.03 + 0.01 + 0.02) / 6 - (1 / 3) * 0.001
    )


def test_zero_signal_moves_only_that_exposure_to_cash() -> None:
    outcome = simulate_monthly_portfolios(
        (_row(1, btc=0.10, paxg=0.06, equity=0.03, cash=0.01, directions=(1, 0, -1)),),
        friction_bps=0,
        stress_friction_bps=0,
    )[0]

    assert outcome.candidate_turnover == pytest.approx(1 / 3)
    assert outcome.candidate_return == pytest.approx((2 / 3) * 0.01 + 0.10 / 6 - 0.03 / 6)


def test_diversified_trend_rejects_incomplete_monthly_input() -> None:
    row = MonthlyTrendInput(
        month=date(2026, 1, 1),
        returns={"BTC": 0.01, "PAXG": 0.01, "CASH": 0.001},
        directions={"BTC": 1, "PAXG": 1, "US_EQUITY": 1},
        cpi_level=100,
    )

    with pytest.raises(ValueError, match="收益覆盖不完整"):
        simulate_monthly_portfolios((row,), friction_bps=10, stress_friction_bps=20)
