from __future__ import annotations

from typing import Protocol

from app.domain.order import ApprovedOrder, Order


class Broker(Protocol):
    def submit_order(self, order: ApprovedOrder) -> Order: ...

    def cancel_order(self, instrument_id: str, order_id: str) -> Order: ...

    def get_order(self, instrument_id: str, order_id: str) -> Order: ...

    def get_open_orders(self, instrument_id: str | None = None) -> list[Order]: ...
