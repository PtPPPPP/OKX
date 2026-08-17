from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType


def order() -> Order:
    request = OrderRequest(
        "client-1",
        "BTC-USDT",
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100.0"),
        "signal-1",
        datetime.now(UTC),
    )
    return Order(request)


def test_valid_state_transitions() -> None:
    current = order()
    now = datetime.now(UTC)
    current.transition(OrderState.SUBMITTED, at=now)
    current.transition(OrderState.ACCEPTED, at=now)
    current.transition(OrderState.PARTIALLY_FILLED, at=now)
    current.transition(OrderState.FILLED, at=now)
    assert current.state is OrderState.FILLED


def test_invalid_state_transition_is_rejected() -> None:
    with pytest.raises(ValueError, match="非法订单状态转换"):
        order().transition(OrderState.FILLED, at=datetime.now(UTC))


def test_timeout_can_enter_unknown() -> None:
    current = order()
    now = datetime.now(UTC)
    current.transition(OrderState.SUBMITTED, at=now)
    current.transition(OrderState.UNKNOWN, at=now)
    assert current.is_open


def test_unknown_state_blocks_terminal_shortcut() -> None:
    current = order()
    now = datetime.now(UTC)
    current.transition(OrderState.SUBMITTED, at=now)
    current.transition(OrderState.UNKNOWN, at=now)
    with pytest.raises(ValueError):
        current.transition(OrderState.SUBMITTED, at=now)
