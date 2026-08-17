from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.market import Instrument
from app.domain.order import ApprovedOrder, Order, OrderSide, OrderState
from app.domain.position import Portfolio


@dataclass(frozen=True, slots=True)
class Fill:
    run_id: str
    strategy_name: str
    instrument_id: str
    timestamp: datetime
    side: OrderSide
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    notional: Decimal
    fee: Decimal
    slippage_cost: Decimal
    signal_id: str
    client_order_id: str


class BacktestBroker:
    def __init__(
        self,
        portfolio: Portfolio,
        instrument: Instrument,
        fee_rate: Decimal,
        slippage_rate: Decimal,
    ) -> None:
        self.portfolio = portfolio
        self.instrument = instrument
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.fills: list[Fill] = []
        self.closed_trade_pnls: list[Decimal] = []
        self._orders: dict[str, Order] = {}
        self._entry_total_cost: dict[str, Decimal] = {}

    def submit_order(self, approved: ApprovedOrder) -> Order:
        proposed = approved.proposed
        existing = self._orders.get(proposed.client_order_id)
        if existing is not None:
            return existing
        if proposed.instrument_id != self.instrument.instrument_id:
            raise ValueError("Broker 收到不匹配的交易品种")
        order = Order(proposed.to_request())
        order.transition(OrderState.SUBMITTED, at=approved.approved_at)
        multiplier = (
            Decimal("1") + self.slippage_rate
            if proposed.side is OrderSide.BUY
            else Decimal("1") - self.slippage_rate
        )
        fill_price = proposed.price * multiplier
        notional = fill_price * proposed.quantity
        fee = notional * self.fee_rate
        quote = self.instrument.quote_currency
        base = self.instrument.base_currency
        instrument_id = self.instrument.instrument_id
        position_before = self.portfolio.positions.get(instrument_id, Decimal("0"))

        if proposed.side is OrderSide.BUY:
            total_cost = notional + fee
            if total_cost > self.portfolio.balances.get(quote, Decimal("0")):
                raise ValueError("回测成交会导致计价币余额为负")
            self.portfolio.balances[quote] -= total_cost
            self.portfolio.positions[instrument_id] = position_before + proposed.quantity
            self.portfolio.balances[base] = (
                self.portfolio.balances.get(base, Decimal("0")) + proposed.quantity
            )
            self.portfolio.average_entry_prices[instrument_id] = fill_price
            self._entry_total_cost[instrument_id] = total_cost
        else:
            if proposed.quantity > position_before:
                raise ValueError("回测成交会导致基础币持仓为负")
            self.portfolio.balances[quote] = (
                self.portfolio.balances.get(quote, Decimal("0")) + notional - fee
            )
            remaining = position_before - proposed.quantity
            self.portfolio.positions[instrument_id] = remaining
            self.portfolio.balances[base] = (
                self.portfolio.balances.get(base, Decimal("0")) - proposed.quantity
            )
            entry_cost = self._entry_total_cost.get(instrument_id, Decimal("0"))
            allocated_cost = (
                entry_cost * proposed.quantity / position_before
                if position_before > 0
                else Decimal("0")
            )
            pnl = notional - fee - allocated_cost
            self.portfolio.realized_pnl += pnl
            self.closed_trade_pnls.append(pnl)
            if remaining == 0:
                self.portfolio.average_entry_prices.pop(instrument_id, None)
                self._entry_total_cost.pop(instrument_id, None)
            else:
                self._entry_total_cost[instrument_id] = entry_cost - allocated_cost

        order.filled_quantity = proposed.quantity
        order.average_price = fill_price
        order.transition(OrderState.FILLED, at=approved.approved_at)
        self._orders[proposed.client_order_id] = order
        self.fills.append(
            Fill(
                run_id=proposed.run_id,
                strategy_name=proposed.strategy_name,
                instrument_id=proposed.instrument_id,
                timestamp=approved.approved_at,
                side=proposed.side,
                quantity=proposed.quantity,
                reference_price=proposed.price,
                fill_price=fill_price,
                notional=notional,
                fee=fee,
                slippage_cost=abs(fill_price - proposed.price) * proposed.quantity,
                signal_id=proposed.signal_id,
                client_order_id=proposed.client_order_id,
            )
        )
        return order

    def cancel_order(self, instrument_id: str, order_id: str) -> Order:
        order = self.get_order(instrument_id, order_id)
        if not order.is_open:
            return order
        raise ValueError("回测 Broker 当前没有可撤销的未成交订单")

    def get_order(self, instrument_id: str, order_id: str) -> Order:
        try:
            order = self._orders[order_id]
        except KeyError as exc:
            raise ValueError(f"回测订单不存在: {order_id}") from exc
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
