from __future__ import annotations

from decimal import Decimal
from math import sqrt


def maximum_drawdown(equities: list[Decimal]) -> Decimal:
    if not equities:
        return Decimal("0")
    peak = equities[0]
    maximum = Decimal("0")
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak * Decimal("100"))
    return maximum


def sharpe_ratio(returns: list[Decimal], periods_per_year: float) -> Decimal:
    if len(returns) < 2:
        return Decimal("0")
    values = [float(value) for value in returns]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance == 0:
        return Decimal("0")
    return Decimal(str(mean / sqrt(variance) * sqrt(periods_per_year)))
