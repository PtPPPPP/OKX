from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.order import OrderSide
from app.domain.position import PositionCost
from app.portfolio.cost_basis import CostFill, recover_average_cost

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def fill(
    side: OrderSide,
    quantity: str,
    price: str,
    *,
    fee: str = "0",
    fee_currency: str | None = None,
    offset: int = 0,
) -> CostFill:
    return CostFill(
        side,
        Decimal(quantity),
        Decimal(price),
        Decimal(fee),
        fee_currency,
        NOW + timedelta(seconds=offset),
    )


def recover(fills: list[CostFill], current: str) -> PositionCost:
    return recover_average_cost(
        fills,
        current_quantity=Decimal(current),
        base_currency="BTC",
        quote_currency="USDT",
        quantity_tolerance=Decimal("0.00000001"),
        source="test_fills",
    )


def test_single_buy_cost() -> None:
    cost = recover([fill(OrderSide.BUY, "1", "100")], "1")
    assert cost.cost_is_reliable
    assert cost.average_entry_price == Decimal("100")


def test_multiple_buys_use_weighted_cost() -> None:
    cost = recover(
        [
            fill(OrderSide.BUY, "1", "100"),
            fill(OrderSide.BUY, "1", "200", offset=1),
        ],
        "2",
    )
    assert cost.average_entry_price == Decimal("150")


def test_partial_sell_keeps_remaining_average_cost() -> None:
    cost = recover(
        [
            fill(OrderSide.BUY, "1", "100"),
            fill(OrderSide.BUY, "1", "200", offset=1),
            fill(OrderSide.SELL, "1", "250", offset=2),
        ],
        "1",
    )
    assert cost.average_entry_price == Decimal("150")


def test_quote_fee_increases_cost_and_base_fee_reduces_acquired_quantity() -> None:
    quote_fee = recover(
        [fill(OrderSide.BUY, "1", "100", fee="1", fee_currency="USDT")],
        "1",
    )
    base_fee = recover(
        [fill(OrderSide.BUY, "1", "100", fee="0.01", fee_currency="BTC")],
        "0.99",
    )
    assert quote_fee.average_entry_price == Decimal("101")
    assert base_fee.average_entry_price == Decimal("100") / Decimal("0.99")


def test_incomplete_history_marks_cost_unreliable() -> None:
    cost = recover([fill(OrderSide.BUY, "1", "100")], "2")
    assert not cost.cost_is_reliable
    assert cost.average_entry_price is None
