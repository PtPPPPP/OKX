from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.order import (
    Order,
    OrderRequest,
    OrderSide,
    OrderSource,
    OrderState,
    OrderType,
)
from app.portfolio.cost_basis import CostFill
from app.storage.database import Database


def canonical_fill_id(
    client_order_id: str,
    side: OrderSide,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    filled_at: datetime,
) -> str:
    identity = ":".join(
        (
            client_order_id,
            side.value,
            str(quantity),
            str(price),
            str(fee),
            filled_at.isoformat(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _state_regresses(current: OrderState, incoming: OrderState) -> bool:
    if current is incoming:
        return False
    terminal = {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
    if current in terminal:
        return True
    if incoming is OrderState.UNKNOWN:
        return False
    if current is OrderState.UNKNOWN:
        return incoming in {OrderState.CREATED, OrderState.SUBMITTED}
    rank = {
        OrderState.CREATED: 0,
        OrderState.SUBMITTED: 1,
        OrderState.ACCEPTED: 2,
        OrderState.PARTIALLY_FILLED: 3,
        OrderState.CANCEL_PENDING: 3,
        OrderState.FILLED: 4,
        OrderState.CANCELLED: 4,
        OrderState.REJECTED: 4,
        OrderState.EXPIRED: 4,
    }
    return rank[incoming] < rank[current]


class OrderRepository:
    """Orders, state history and idempotent fills."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def save_order(self, order: Order) -> None:
        updated_at = (order.updated_at or order.request.created_at).isoformat()
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (order.request.client_order_id,),
            ).fetchone()
            if current is not None and _state_regresses(
                OrderState(str(current["state"])), order.state
            ):
                raise ValueError(f"拒绝订单状态回退: {current['state']} -> {order.state.value}")
            connection.execute(
                """INSERT INTO orders
                (client_order_id, exchange_order_id, instrument_id, side, order_type,
                 quantity, price, signal_id, state, filled_quantity, average_price,
                 created_at, updated_at, run_id, mode, strategy_name, bar, order_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                  exchange_order_id=excluded.exchange_order_id,
                  state=excluded.state, filled_quantity=excluded.filled_quantity,
                  average_price=excluded.average_price, updated_at=excluded.updated_at""",
                (
                    order.request.client_order_id,
                    order.exchange_order_id,
                    order.request.instrument_id,
                    order.request.side.value,
                    order.request.order_type.value,
                    str(order.request.quantity),
                    str(order.request.price),
                    order.request.signal_id,
                    order.state.value,
                    str(order.filled_quantity),
                    str(order.average_price) if order.average_price is not None else None,
                    order.request.created_at.isoformat(),
                    updated_at,
                    order.request.run_id,
                    order.request.mode,
                    order.request.strategy_name,
                    order.request.bar,
                    order.request.order_source.value,
                ),
            )
            audit_run_id = str(current["run_id"]) if current is not None else order.request.run_id
            audit_mode = str(current["mode"]) if current is not None else order.request.mode
            audit_strategy = (
                str(current["strategy_name"])
                if current is not None
                else order.request.strategy_name
            )
            audit_bar = str(current["bar"]) if current is not None else order.request.bar
            audit_signal_id = (
                str(current["signal_id"]) if current is not None else order.request.signal_id
            )
            audit_source = (
                str(current["order_source"])
                if current is not None
                else order.request.order_source.value
            )
            connection.execute(
                """INSERT OR IGNORE INTO order_state_changes
                (client_order_id, state, changed_at, run_id, mode, strategy_name,
                 instrument_id, bar, signal_id, exchange_order_id, order_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.request.client_order_id,
                    order.state.value,
                    updated_at,
                    audit_run_id,
                    audit_mode,
                    audit_strategy,
                    order.request.instrument_id,
                    audit_bar,
                    audit_signal_id,
                    order.exchange_order_id or "",
                    audit_source,
                ),
            )

    def load_order(self, client_order_id: str) -> Order | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def load_open_orders(self) -> list[Order]:
        terminal = tuple(
            state.value
            for state in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            )
        )
        placeholders = ",".join("?" for _ in terminal)
        with self.database.connect() as connection:
            query = (
                f"SELECT * FROM orders WHERE state NOT IN ({placeholders}) ORDER BY updated_at DESC"
            )
            rows = connection.execute(query, terminal).fetchall()
        return [self._row_to_order(row) for row in rows]

    def load_recent_order_times(self, since: datetime) -> tuple[datetime, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT created_at FROM orders WHERE created_at >= ? ORDER BY created_at",
                (since.isoformat(),),
            ).fetchall()
        return tuple(datetime.fromisoformat(str(row["created_at"])) for row in rows)

    def load_cost_fills(self, instrument_id: str) -> list[CostFill]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT f.side, f.quantity, f.price, f.fee, f.fee_currency,
                          f.filled_at
                FROM fills AS f
                JOIN orders AS o ON o.client_order_id = f.client_order_id
                WHERE o.instrument_id = ? AND o.mode = 'demo'
                  AND f.eligible_for_cost_basis = 1 AND f.mode = 'demo'
                ORDER BY f.filled_at""",
                (instrument_id,),
            ).fetchall()
        return [
            CostFill(
                side=OrderSide(str(row["side"])),
                quantity=Decimal(str(row["quantity"])),
                price=Decimal(str(row["price"])),
                fee=Decimal(str(row["fee"])),
                fee_currency=(str(row["fee_currency"]) if row["fee_currency"] else None),
                filled_at=datetime.fromisoformat(str(row["filled_at"])),
            )
            for row in rows
        ]

    def save_fill(
        self,
        client_order_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        filled_at: datetime,
        *,
        fill_id: str | None = None,
        exchange_fill_id: str | None = None,
        fee_currency: str | None = None,
        run_id: str | None = None,
        mode: str | None = None,
    ) -> bool:
        stable_fill_id = fill_id or canonical_fill_id(
            client_order_id, side, quantity, price, fee, filled_at
        )
        with self.database.connect() as connection:
            parent = connection.execute(
                """SELECT mode, run_id FROM orders WHERE client_order_id = ?""",
                (client_order_id,),
            ).fetchone()
            has_parent = parent is not None or (run_id is not None and mode is not None)
            mode = str(parent["mode"]) if parent else str(mode or "")
            run_id = str(parent["run_id"]) if parent else str(run_id or "")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO fills
                (client_order_id, side, quantity, price, fee, filled_at,
                 fill_id, exchange_fill_id, fee_currency, data_quality_status,
                 quarantine_reason, eligible_for_cost_basis, source, mode, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    client_order_id,
                    side.value,
                    str(quantity),
                    str(price),
                    str(fee),
                    filled_at.isoformat(),
                    stable_fill_id,
                    exchange_fill_id,
                    fee_currency,
                    "trusted" if has_parent else "quarantined",
                    None if has_parent else "missing_parent_order",
                    int(has_parent and mode == "demo"),
                    "private_event" if has_parent else "orphan_event",
                    mode,
                    run_id,
                ),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _row_to_order(row: Any) -> Order:
        request = OrderRequest(
            client_order_id=str(row["client_order_id"]),
            instrument_id=str(row["instrument_id"]),
            side=OrderSide(str(row["side"])),
            order_type=OrderType(str(row["order_type"])),
            quantity=Decimal(str(row["quantity"])),
            price=Decimal(str(row["price"])),
            signal_id=str(row["signal_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            run_id=str(row["run_id"]),
            strategy_name=str(row["strategy_name"]),
            mode=str(row["mode"]),
            bar=str(row["bar"]),
            order_source=OrderSource(str(row["order_source"])),
        )
        state = OrderState(str(row["state"]))
        return Order(
            request=request,
            state=state,
            exchange_order_id=str(row["exchange_order_id"]) if row["exchange_order_id"] else None,
            filled_quantity=Decimal(str(row["filled_quantity"])),
            average_price=Decimal(str(row["average_price"])) if row["average_price"] else None,
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            status_history=[state],
        )
