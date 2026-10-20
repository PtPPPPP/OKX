"""Offline-only allocation and ownership around the existing OHLC broker.

Strategy books are the authoritative partitions of one account. The account is
their sum, not a second independently updated cost ledger. Matching and every
partition's cost calculation remain BacktestBroker's existing implementation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from app.domain.context import MarketSnapshot
from app.domain.market import Candle, Instrument
from app.domain.order import ApprovedOrder, Order, OrderSide
from app.domain.position import Portfolio, PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.execution.backtest_broker import BacktestBroker, Fill
from app.position_sizing.base import PositionSizeDecision, PositionSizer
from app.risk.position_sizing import quantize_down

ZERO = Decimal("0")


class PortfolioBacktestBroker(BacktestBroker):
    """One matching queue/volume pool; fixed, non-transferable cash partitions."""

    def __init__(
        self,
        instrument: Instrument,
        initial_capital: Decimal,
        allocations: dict[str, Decimal],
        fee_rate: Decimal,
        slippage_rate: Decimal,
        *,
        volume_participation: Decimal = Decimal("1"),
    ) -> None:
        if (
            not initial_capital.is_finite()
            or initial_capital <= 0
            or not allocations
            or any(
                not key or not value.is_finite() or value <= 0 for key, value in allocations.items()
            )
            or sum(allocations.values(), ZERO) > initial_capital
        ):
            raise ValueError("invalid fixed portfolio allocations / initial capital")
        super().__init__(
            Portfolio({instrument.quote_currency: initial_capital}),
            instrument,
            fee_rate,
            slippage_rate,
            volume_participation=volume_participation,
        )
        self.allocations = dict(sorted(allocations.items()))
        self.unallocated_cash = initial_capital - sum(allocations.values(), ZERO)
        self.books = {
            key: BacktestBroker(
                Portfolio({instrument.quote_currency: amount}), instrument, fee_rate, slippage_rate
            )
            for key, amount in self.allocations.items()
        }
        self._applied: dict[tuple[str, datetime], Fill] = {}
        self._sync_account()

    def orders_for(self, instance_id: str) -> list[Order]:
        return [
            order
            for order in self.get_open_orders()
            if order.request.strategy_instance_id == instance_id
        ]

    def reserved_cash(self, instance_id: str | None = None) -> Decimal:
        orders = self.get_open_orders() if instance_id is None else self.orders_for(instance_id)
        return sum(
            (
                (order.request.quantity - order.filled_quantity)
                * order.request.price
                * (Decimal("1") + self.fee_rate)
                for order in orders
                if order.request.side is OrderSide.BUY
            ),
            ZERO,
        )

    def available_cash(self, instance_id: str | None = None) -> Decimal:
        portfolio = self.portfolio if instance_id is None else self.books[instance_id].portfolio
        return portfolio.balances[self.instrument.quote_currency] - self.reserved_cash(instance_id)

    def available_quantity(self, instance_id: str) -> Decimal:
        reserved = sum(
            (
                order.request.quantity - order.filled_quantity
                for order in self.orders_for(instance_id)
                if order.request.side is OrderSide.SELL
            ),
            ZERO,
        )
        return (
            self.books[instance_id].portfolio.positions.get(self.instrument.instrument_id, ZERO)
            - reserved
        )

    def submit_order(self, approved: ApprovedOrder) -> Order:
        if approved.proposed.strategy_instance_id not in self.books:
            raise ValueError("unknown strategy instance; account ownership is mandatory")
        existing = approved.proposed.client_order_id in self._orders
        try:
            return super().submit_order(approved)
        except BaseException:
            # This broker has no external I/O: an exception before any local fill
            # is a known failed submission. Release only this new reservation.
            order = self._orders.get(approved.proposed.client_order_id)
            if not existing and order is not None and order.is_open and order.filled_quantity == 0:
                self.cancel_order(order.request.instrument_id, order.request.client_order_id)
            raise

    def _can_reserve(self, order: Order) -> bool:
        request = order.request
        owner = request.strategy_instance_id
        if request.side is OrderSide.BUY:
            needed = request.notional * (Decimal("1") + self.fee_rate)
            return needed <= min(self.available_cash(), self.available_cash(owner))
        return request.quantity <= self.available_quantity(owner)

    def _fill(
        self, order: Order, quantity: Decimal, reference: Decimal, price: Decimal, at: datetime
    ) -> None:
        request = order.request
        reason = (
            self._exit_reasons.get(request.signal_id, "strategy_exit")
            if request.side is OrderSide.SELL
            else None
        )
        fill = Fill(
            request.run_id,
            request.strategy_name,
            request.instrument_id,
            at,
            request.side,
            quantity,
            reference,
            price,
            price * quantity,
            price * quantity * self.fee_rate,
            abs(price - reference) * quantity,
            request.signal_id,
            request.client_order_id,
            reason,
            request.strategy_instance_id,
        )
        self.apply_fill(fill)

    def apply_fill(self, fill: Fill) -> bool:
        """Idempotent offline fill accounting, never an exchange feedback ingress.

        The OHLC model emits at most one fill per order per phase timestamp.
        Duplicate payloads are no-ops; conflicting payloads fail closed.
        """
        key = (fill.client_order_id, fill.timestamp)
        previous = self._applied.get(key)
        if previous is not None:
            if previous != fill:
                raise ValueError("conflicting duplicate fill")
            return False
        order = self.get_order(fill.instrument_id, fill.client_order_id)
        request = order.request
        if (
            not order.is_open
            or fill.strategy_instance_id != request.strategy_instance_id
            or fill.strategy_name != request.strategy_name
            or fill.run_id != request.run_id
            or fill.side is not request.side
            or fill.signal_id != request.signal_id
            or any(
                not value.is_finite()
                for value in (
                    fill.quantity,
                    fill.fill_price,
                    fill.notional,
                    fill.fee,
                    fill.reference_price,
                    fill.slippage_cost,
                )
            )
            or fill.quantity <= 0
            or fill.fill_price <= 0
            or fill.reference_price <= 0
            or fill.quantity > request.quantity - order.filled_quantity
            or fill.quantity % self.instrument.quantity_step
            or fill.fill_price % self.instrument.price_tick
            or fill.notional != fill.fill_price * fill.quantity
            or fill.fee != fill.notional * self.fee_rate
        ):
            raise ValueError("invalid or unowned portfolio fill")
        book = self.books[fill.strategy_instance_id]
        if request.side is OrderSide.SELL:
            book._exit_reasons[request.signal_id] = fill.exit_reason or "strategy_exit"
        # Reuse the existing weighted cost, quote-fee and completed-cycle model.
        book._fill(order, fill.quantity, fill.reference_price, fill.fill_price, fill.timestamp)
        self._applied[key] = fill
        self.fills.append(fill)
        self.closed_trades = [trade for item in self.books.values() for trade in item.closed_trades]
        self.closed_trade_pnls = [trade.pnl for trade in self.closed_trades]
        self._sync_account()
        return True

    def _sync_account(self) -> None:
        instrument_id = self.instrument.instrument_id
        quote, base = self.instrument.quote_currency, self.instrument.base_currency
        quantity = sum(
            (b.portfolio.positions.get(instrument_id, ZERO) for b in self.books.values()), ZERO
        )
        cost = sum((b._position_notional for b in self.books.values()), ZERO)
        self.portfolio.balances[quote] = self.unallocated_cash + sum(
            (b.portfolio.balances[quote] for b in self.books.values()), ZERO
        )
        self.portfolio.balances[base] = quantity
        self.portfolio.positions[instrument_id] = quantity
        self.portfolio.realized_pnl = sum(
            (b.portfolio.realized_pnl for b in self.books.values()), ZERO
        )
        if quantity:
            self.portfolio.average_entry_prices[instrument_id] = cost / quantity
        else:
            self.portfolio.average_entry_prices.pop(instrument_id, None)
        self._position_notional = cost


class StrategyBroker:
    """Ownership-scoped broker view injected into each existing BacktestEngine."""

    def __init__(self, shared: PortfolioBacktestBroker, instance_id: str) -> None:
        self.shared, self.instance_id = shared, instance_id
        self.portfolio = shared.books[instance_id].portfolio
        self.fills = shared.books[instance_id].fills
        self.closed_trades = shared.books[instance_id].closed_trades
        self.closed_trade_pnls = shared.books[instance_id].closed_trade_pnls

    def register_signal(self, signal: Signal) -> None:
        self.shared.register_signal(signal)

    def submit_order(self, approved: ApprovedOrder) -> Order:
        if approved.proposed.strategy_instance_id != self.instance_id:
            raise ValueError("strategy cannot submit another instance's order")
        return self.shared.submit_order(approved)

    def get_open_orders(self, instrument_id: str | None = None) -> list[Order]:
        return [
            order
            for order in self.shared.orders_for(self.instance_id)
            if instrument_id is None or order.request.instrument_id == instrument_id
        ]

    def get_order(self, instrument_id: str, order_id: str) -> Order:
        order = self.shared.get_order(instrument_id, order_id)
        if order.request.strategy_instance_id != self.instance_id:
            raise ValueError("strategy cannot access another instance's order")
        return order

    def cancel_order(self, instrument_id: str, order_id: str) -> Order:
        self.get_order(instrument_id, order_id)
        return self.shared.cancel_order(instrument_id, order_id)

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return self.shared.books[self.instance_id].unrealized_pnl(mark_price)

    def begin_bar(self, candle: Candle) -> None:
        raise RuntimeError("market phases belong to the portfolio runner")

    def begin_intrabar(self, at: datetime) -> None:
        raise RuntimeError("market phases belong to the portfolio runner")

    def match_orders(self) -> list[Order]:
        raise RuntimeError("shared matching belongs to the portfolio runner")


class BudgetPositionSizer:
    """Allocate an intention without changing the underlying strategy/sizer."""

    name = "fixed_partition_budget"

    def __init__(self, delegate: PositionSizer, broker: StrategyBroker) -> None:
        self.delegate, self.broker = delegate, broker

    def calculate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        instrument: Instrument,
    ) -> PositionSizeDecision:
        requested = self.delegate.calculate(signal, portfolio, market, instrument)
        shared, owner = self.broker.shared, self.broker.instance_id
        if signal.action is SignalAction.BUY:
            # LIMIT buys are capped at their limit; slippage cannot exceed it.
            cash = max(ZERO, min(shared.available_cash(), shared.available_cash(owner)))
            maximum = quantize_down(
                cash / (market.price * (1 + shared.fee_rate)), instrument.quantity_step
            )
        else:
            maximum = quantize_down(shared.available_quantity(owner), instrument.quantity_step)
        quantity = min(requested.quantity, maximum)
        return replace(
            requested,
            quantity=quantity,
            notional=quantity * market.price,
            reason=f"{requested.reason}; fixed partition {owner}; requested={requested.quantity}",
        )
