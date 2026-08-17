from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.order import OrderSide
from app.domain.position import PositionCost


@dataclass(frozen=True, slots=True)
class CostFill:
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str | None
    filled_at: datetime


def recover_average_cost(
    fills: list[CostFill],
    *,
    current_quantity: Decimal,
    base_currency: str,
    quote_currency: str,
    quantity_tolerance: Decimal,
    source: str,
) -> PositionCost:
    if current_quantity <= 0:
        return PositionCost(None, "no_position", False)
    quantity = Decimal("0")
    total_cost = Decimal("0")
    reliable = True
    for fill in sorted(fills, key=lambda item: item.filled_at):
        if any(
            not value.is_finite() or value < 0 for value in (fill.quantity, fill.price, fill.fee)
        ):
            return PositionCost(None, f"{source}_invalid_fill", False)
        if fill.quantity <= 0 or fill.price <= 0:
            return PositionCost(None, f"{source}_invalid_fill", False)
        if fill.side is OrderSide.BUY:
            acquired = fill.quantity
            added_cost = fill.quantity * fill.price
            if fill.fee_currency == base_currency:
                acquired -= fill.fee
            elif fill.fee_currency == quote_currency:
                added_cost += fill.fee
            elif fill.fee > 0:
                reliable = False
            if acquired <= 0:
                return PositionCost(None, f"{source}_invalid_fee", False)
            quantity += acquired
            total_cost += added_cost
            continue
        reduction = fill.quantity + (
            fill.fee if fill.fee_currency == base_currency else Decimal("0")
        )
        if reduction > quantity + quantity_tolerance or quantity <= 0:
            return PositionCost(None, f"{source}_incomplete_history", False)
        remaining = max(quantity - reduction, Decimal("0"))
        total_cost = total_cost * remaining / quantity
        quantity = remaining
        if fill.fee > 0 and fill.fee_currency not in {base_currency, quote_currency}:
            reliable = False
    if abs(quantity - current_quantity) > quantity_tolerance or quantity <= 0:
        return PositionCost(None, f"{source}_quantity_mismatch", False)
    average = total_cost / quantity
    if not reliable or average <= 0:
        return PositionCost(None, f"{source}_unreliable", False)
    return PositionCost(average, source, True)
