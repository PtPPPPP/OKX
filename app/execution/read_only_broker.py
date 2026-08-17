from __future__ import annotations

from app.domain.order import ApprovedOrder, Order


class ReadOnlyBroker:
    """Provides open-order context while making submission structurally impossible."""

    def __init__(self, open_orders: tuple[Order, ...] = ()) -> None:
        self._orders = {order.request.client_order_id: order for order in open_orders}

    def submit_order(self, order: ApprovedOrder) -> Order:
        raise RuntimeError("只读执行通道禁止提交订单")

    def cancel_order(self, instrument_id: str, order_id: str) -> Order:
        raise RuntimeError("只读执行通道禁止撤单")

    def get_order(self, instrument_id: str, order_id: str) -> Order:
        try:
            order = self._orders[order_id]
        except KeyError as exc:
            raise ValueError(f"只读执行通道未找到订单: {order_id}") from exc
        if order.request.instrument_id != instrument_id:
            raise ValueError("订单交易品种不匹配")
        return order

    def get_open_orders(self, instrument_id: str | None = None) -> list[Order]:
        return [
            order
            for order in self._orders.values()
            if order.is_open
            and (instrument_id is None or order.request.instrument_id == instrument_id)
        ]
