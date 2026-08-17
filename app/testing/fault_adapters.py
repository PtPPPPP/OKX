from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from typing import Protocol, TypeVar

from app.domain.market import Candle, Instrument
from app.domain.order import Order
from app.domain.position import PortfolioSnapshot
from app.market.websocket import WebSocketLike
from app.portfolio.cost_basis import CostFill
from app.testing.fault_injection import FaultInjector, FaultPlan, VirtualClock

_Result = TypeVar("_Result")


class LocalReconciliationClient(Protocol):
    is_local_adapter: bool
    broker_write_calls: int
    external_network_calls: int

    def get_portfolio(self, instrument: Instrument) -> PortfolioSnapshot: ...

    def get_pending_orders(self, instrument_id: str) -> list[Order]: ...

    def get_history_candles(
        self,
        instrument_id: str,
        bar: str = "5m",
        limit: int = 300,
    ) -> list[Candle]: ...

    def query_order(self, instrument_id: str, client_order_id: str) -> Order: ...

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]: ...

    def get_derivative_positions(self) -> dict[str, Decimal]: ...


class FaultInjectingRestClient:
    """Local-only decorator for every private REST read used by reconciliation."""

    is_local_adapter = True

    def __init__(
        self,
        delegate: LocalReconciliationClient,
        plan: FaultPlan,
        clock: VirtualClock,
    ) -> None:
        if not getattr(delegate, "is_local_adapter", False):
            raise ValueError("REST fault injection requires an explicit local delegate")
        self.delegate = delegate
        self.injector = FaultInjector(plan, self, clock)

    @property
    def broker_write_calls(self) -> int:
        return self.delegate.broker_write_calls

    @property
    def external_network_calls(self) -> int:
        return self.delegate.external_network_calls

    def get_portfolio(self, instrument: Instrument) -> PortfolioSnapshot:
        return self._read(lambda: self.delegate.get_portfolio(instrument))

    def get_pending_orders(self, instrument_id: str) -> list[Order]:
        return self._read(lambda: self.delegate.get_pending_orders(instrument_id))

    def get_history_candles(
        self,
        instrument_id: str,
        bar: str = "5m",
        limit: int = 300,
    ) -> list[Candle]:
        return self._read(lambda: self.delegate.get_history_candles(instrument_id, bar, limit))

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        return self._read(lambda: self.delegate.query_order(instrument_id, client_order_id))

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]:
        return self._read(lambda: self.delegate.get_trade_fills(instrument_id))

    def get_derivative_positions(self) -> dict[str, Decimal]:
        return self._read(self.delegate.get_derivative_positions)

    def _read(self, operation: Callable[[], _Result]) -> _Result:
        self.injector.inject("private_rest.request.before_send")
        result = operation()
        self.injector.inject("private_rest.response.before_parse")
        self.injector.inject("private_rest.snapshot.before_apply")
        return result


class LocalConnectionFactory(Protocol):
    is_local_adapter: bool

    def __call__(self, url: str) -> AbstractAsyncContextManager[WebSocketLike]: ...


class FaultInjectingWebSocketFactory:
    """Local-only connection decorator used to drive deterministic reconnects."""

    is_local_adapter = True

    def __init__(
        self,
        delegate: LocalConnectionFactory,
        plan: FaultPlan,
        clock: VirtualClock,
    ) -> None:
        if not getattr(delegate, "is_local_adapter", False):
            raise ValueError("WebSocket fault injection requires an explicit local delegate")
        self.delegate = delegate
        self.injector = FaultInjector(plan, self, clock)

    def __call__(self, url: str) -> AbstractAsyncContextManager[WebSocketLike]:
        return self._connect(url)

    @asynccontextmanager
    async def _connect(self, url: str) -> AsyncIterator[WebSocketLike]:
        self.injector.inject("private_ws.connect.before")
        async with self.delegate(url) as socket:
            yield _FaultInjectingSocket(socket, self.injector)


class _FaultInjectingSocket:
    def __init__(self, delegate: WebSocketLike, injector: FaultInjector) -> None:
        self.delegate = delegate
        self.injector = injector

    async def send(self, message: str) -> None:
        await self.delegate.send(message)

    async def recv(self) -> str | bytes:
        self.injector.inject("private_ws.receive.before")
        return await self.delegate.recv()

    async def close(self) -> None:
        await self.delegate.close()
