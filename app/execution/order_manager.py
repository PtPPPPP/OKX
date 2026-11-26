from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from uuid import uuid4

from app.domain.order import OrderSide
from app.runtime.clock import Clock, SystemClock


def new_client_order_id(clock: Clock | None = None) -> str:
    timestamp = (clock or SystemClock()).now().strftime("%Y%m%d%H%M%S")
    return f"oq{timestamp}{uuid4().hex[:12]}"


def resolve_bbo_maker_price(
    side: OrderSide,
    reference_price: Decimal,
    *,
    bid_price: Decimal | None,
    ask_price: Decimal | None,
    price_tick: Decimal,
) -> Decimal:
    """Clamp limit price to the passive side of BBO so a POST_ONLY order never crosses the spread."""
    if not price_tick.is_finite() or price_tick <= 0:
        raise ValueError("price_tick 必须大于 0")
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("reference_price 必须大于 0")

    target = reference_price
    if side is OrderSide.BUY:
        if ask_price is not None and ask_price.is_finite() and ask_price > price_tick:
            maker_ceiling = ask_price - price_tick
            target = min(target, maker_ceiling)
        quantized = (target / price_tick).to_integral_value(rounding=ROUND_DOWN) * price_tick
        return max(quantized, price_tick)

    if bid_price is not None and bid_price.is_finite() and bid_price > 0:
        maker_floor = bid_price + price_tick
        target = max(target, maker_floor)
    quantized = (target / price_tick).to_integral_value(rounding=ROUND_CEILING) * price_tick
    return max(quantized, price_tick)
