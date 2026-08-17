from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import Event

from app.domain.market import Candle, Instrument
from app.domain.order import Order
from app.domain.position import PortfolioSnapshot
from app.exchange.exceptions import OrderNotFound
from app.portfolio.cost_basis import CostFill


@dataclass(frozen=True, slots=True)
class RestCallGate:
    entered: Event
    release: Event


class ProgrammableExchange:
    """Local-only reconciliation client with deterministic REST fault controls.

    It deliberately implements no order-write method. Tests can block exactly
    one REST snapshot call, replace the next result with an exception, or
    return a stale order list while private events continue through the
    coordinator.
    """

    is_local_adapter = True

    def __init__(
        self,
        portfolio: PortfolioSnapshot,
        candles: list[Candle],
        *,
        pending_orders: list[Order] | None = None,
        derivative_positions: dict[str, Decimal] | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.candles = candles
        self.pending_orders = pending_orders or []
        self.derivative_positions = derivative_positions or {}
        self.queried_orders: dict[str, Order] = {}
        self._pending_gate: RestCallGate | None = None
        self._pending_failure: Exception | None = None
        self.broker_write_calls = 0
        self.external_network_calls = 0

    def block_next_pending_orders(self) -> RestCallGate:
        gate = RestCallGate(Event(), Event())
        self._pending_gate = gate
        return gate

    def fail_next_pending_orders(self, error: Exception) -> None:
        self._pending_failure = error

    def get_portfolio(self, instrument: Instrument) -> PortfolioSnapshot:
        return self.portfolio

    def get_pending_orders(self, instrument_id: str) -> list[Order]:
        gate, self._pending_gate = self._pending_gate, None
        if gate is not None:
            gate.entered.set()
            if not gate.release.wait(timeout=2):
                raise RuntimeError("programmed REST gate was not released")
        failure, self._pending_failure = self._pending_failure, None
        if failure is not None:
            raise failure
        return list(self.pending_orders)

    def get_history_candles(
        self, instrument_id: str, bar: str = "5m", limit: int = 300
    ) -> list[Candle]:
        return self.candles[-limit:]

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        try:
            return self.queried_orders[client_order_id]
        except KeyError as exc:
            raise OrderNotFound("programmed order is absent") from exc

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]:
        return []

    def get_derivative_positions(self) -> dict[str, Decimal]:
        return self.derivative_positions
