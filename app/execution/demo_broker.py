from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from app.domain.order import ApprovedOrder, Order, OrderRequest, OrderState
from app.exchange.exceptions import OrderRejected
from app.execution.demo_write_authorization import DemoWriteAuthorization
from app.runtime.clock import Clock
from app.storage.repositories import TradingRepository

logger = logging.getLogger("trading.orders")


class DemoBrokerClient(Protocol):
    @property
    def clock(self) -> Clock: ...

    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order: ...

    def query_order(self, instrument_id: str, client_order_id: str) -> Order: ...

    def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order: ...

    def get_pending_orders(self, instrument_id: str) -> list[Order]: ...


class OKXDemoBroker:
    def __init__(
        self,
        client: DemoBrokerClient,
        repository: TradingRepository,
        *,
        before_remote_submit: Callable[[OrderRequest], None] | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.before_remote_submit = before_remote_submit

    def submit_order(self, approved: ApprovedOrder) -> Order:
        raise PermissionError(
            "OKXDemoBroker.submit_order is not a controlled Demo write entry; use a Proposal gate"
        )

    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        if authorization is None:
            raise PermissionError("OKXDemoBroker requires explicit one-use authorization")
        authorization.assert_place_matches(request)
        order = Order(request=request)
        self.repository.save_order(order)
        if self.before_remote_submit is not None:
            self.before_remote_submit(request)
        try:
            placed = self.client.place_order(request, authorization=authorization)
        except OrderRejected:
            order.transition(OrderState.REJECTED, at=self.client.clock.now())
            self.repository.save_order(order)
            logger.warning(
                "模拟盘订单被拒绝",
                extra={
                    "client_order_id": request.client_order_id,
                    "instrument_id": request.instrument_id,
                    "order_state": order.state.value,
                },
            )
            raise
        self.repository.save_order(placed)
        logger.info(
            "模拟盘订单状态已保存",
            extra={
                "client_order_id": request.client_order_id,
                "instrument_id": request.instrument_id,
                "signal_id": request.signal_id,
                "order_state": placed.state.value,
            },
        )
        return placed

    def get_order(self, instrument_id: str, order_id: str) -> Order:
        return self.query_order(instrument_id, order_id)

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        order = self.client.query_order(instrument_id, client_order_id)
        self.repository.save_order(order)
        return order

    def cancel_order(
        self,
        instrument_id: str,
        order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        if authorization is None:
            raise PermissionError("OKXDemoBroker requires explicit one-use authorization")
        authorization.assert_cancel_matches(instrument_id, order_id)
        order = self.client.cancel_order(
            instrument_id,
            order_id,
            authorization=authorization,
        )
        self.repository.save_order(order)
        return order

    def get_open_orders(self, instrument_id: str | None = None) -> list[Order]:
        if instrument_id is None:
            raise ValueError("OKX 模拟盘 MVP 查询挂单时必须指定 instrument_id")
        return self.client.get_pending_orders(instrument_id)


DemoBroker = OKXDemoBroker
