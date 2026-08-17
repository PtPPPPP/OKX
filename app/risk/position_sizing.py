from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("数量步长必须大于 0")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantity_for_notional(notional: Decimal, price: Decimal, step: Decimal) -> Decimal:
    if price <= 0:
        raise ValueError("价格必须大于 0")
    return quantize_down(notional / price, step)
