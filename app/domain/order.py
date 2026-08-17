from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    POST_ONLY = "post_only"
    FOK = "fok"
    IOC = "ioc"


class OrderState(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class OrderSource(StrEnum):
    BACKTEST = "backtest"
    STRATEGY_DEMO = "strategy_demo"
    MANUAL_DEMO_TEST = "manual_demo_test"
    PROTECTIVE_EXIT = "protective_exit"
    RECONCILIATION = "reconciliation"
    ADMINISTRATIVE_CLEANUP = "administrative_cleanup"
    LEGACY = "legacy"


_ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.SUBMITTED, OrderState.REJECTED}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.ACCEPTED, OrderState.FILLED, OrderState.REJECTED, OrderState.UNKNOWN}
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {OrderState.CANCELLED, OrderState.FILLED, OrderState.UNKNOWN}
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal
    signal_id: str
    created_at: datetime
    run_id: str = ""
    strategy_name: str = ""
    mode: str = ""
    bar: str = ""
    order_source: OrderSource = OrderSource.LEGACY

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    client_order_id: str
    run_id: str
    strategy_name: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal
    signal_id: str
    created_at: datetime
    mode: str = ""
    bar: str = ""
    order_source: OrderSource = OrderSource.LEGACY

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price

    def to_request(self) -> OrderRequest:
        return OrderRequest(
            client_order_id=self.client_order_id,
            instrument_id=self.instrument_id,
            side=self.side,
            order_type=self.order_type,
            quantity=self.quantity,
            price=self.price,
            signal_id=self.signal_id,
            created_at=self.created_at,
            run_id=self.run_id,
            strategy_name=self.strategy_name,
            mode=self.mode,
            bar=self.bar,
            order_source=self.order_source,
        )


@dataclass(frozen=True, slots=True)
class ApprovedOrder:
    proposed: ProposedOrder
    approved_at: datetime
    approval_reason: str


@dataclass(slots=True)
class Order:
    request: OrderRequest
    state: OrderState = OrderState.CREATED
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    updated_at: datetime | None = None
    status_history: list[OrderState] = field(default_factory=lambda: [OrderState.CREATED])

    def transition(self, new_state: OrderState, *, at: datetime) -> None:
        if new_state == self.state:
            return
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"非法订单状态转换: {self.state} -> {new_state}")
        self.state = new_state
        self.updated_at = at
        self.status_history.append(new_state)

    @property
    def is_open(self) -> bool:
        return self.state in {
            OrderState.CREATED,
            OrderState.SUBMITTED,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.UNKNOWN,
        }
