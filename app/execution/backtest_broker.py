from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from app.domain.market import Candle, Instrument
from app.domain.order import ApprovedOrder, Order, OrderSide, OrderState, OrderType
from app.domain.position import Portfolio
from app.domain.signal import Signal
from app.market.historical_data import normalize_candles


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
    exit_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    run_id: str
    strategy_name: str
    instrument_id: str
    entered_at: datetime
    exited_at: datetime
    quantity: Decimal
    entry_notional: Decimal
    entry_fees: Decimal
    exit_notional: Decimal
    exit_fees: Decimal
    pnl: Decimal
    holding_time_hours: Decimal
    exit_reason: str


class BacktestBroker:
    """FIFO OHLC limit matching with one shared base-volume budget per bar.

    Close-generated orders start on the next bar. Open fills can improve the
    limit; intrabar touches fill at the limit. Stops are explicit backtest sell
    market orders, never an implicit conversion of an unfilled limit order.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        instrument: Instrument,
        fee_rate: Decimal,
        slippage_rate: Decimal,
        *,
        volume_participation: Decimal = Decimal("1"),
    ) -> None:
        for name, rate in (("fee_rate", fee_rate), ("slippage_rate", slippage_rate)):
            if not rate.is_finite() or not 0 <= rate < 1:
                raise ValueError(f"{name} 必须在 [0, 1) 内")
        if not volume_participation.is_finite() or not 0 < volume_participation <= 1:
            raise ValueError("volume_participation 必须在 (0, 1] 内")
        if portfolio.positions.get(instrument.instrument_id, Decimal("0")) != 0:
            raise ValueError("回测 Broker 必须从空仓开始，以保留完整交易成本")
        self.portfolio = portfolio
        self.instrument = instrument
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.volume_participation = volume_participation
        self.fills: list[Fill] = []
        self.closed_trades: list[ClosedTrade] = []
        self.closed_trade_pnls: list[Decimal] = []
        self._orders: dict[str, Order] = {}
        self._exit_reasons: dict[str, str] = {}
        self._candle: Candle | None = None
        self._at: datetime | None = None
        self._intrabar = False
        self._volume_remaining = Decimal("0")
        self._position_notional = Decimal("0")
        self._entered_at: datetime | None = None
        self._entry_quantity = Decimal("0")
        self._entry_notional = Decimal("0")
        self._entry_fees = Decimal("0")
        self._exit_notional = Decimal("0")
        self._exit_fees = Decimal("0")
        self._trade_exit_reasons: set[str] = set()

    def register_signal(self, signal: Signal) -> None:
        self._exit_reasons[signal.signal_id] = str(
            signal.metadata.get("exit_reason", "strategy_exit")
        )

    def begin_bar(self, candle: Candle) -> None:
        normalized = normalize_candles([candle], bar="1m")[0]
        if not normalized.confirmed:
            raise ValueError("回测成交只接受已确认 K 线")
        if self._candle is not None and normalized.timestamp <= self._candle.timestamp:
            raise ValueError("不能重复或逆序消费 K 线成交量")
        self._candle = normalized
        self._at = normalized.timestamp
        self._intrabar = False
        self._volume_remaining = self._quantity(normalized.volume * self.volume_participation)

    def begin_intrabar(self, at: datetime) -> None:
        if self._candle is None or at <= self._candle.timestamp:
            raise ValueError("盘中成交必须在当前 K 线开始之后")
        self._at = at
        self._intrabar = True

    def match_orders(self) -> list[Order]:
        updates = []
        for order in self.get_open_orders():
            if self._match(order):
                updates.append(order)
        return updates

    def submit_order(self, approved: ApprovedOrder) -> Order:
        proposed = approved.proposed
        existing = self._orders.get(proposed.client_order_id)
        if existing is not None:
            if existing.request != proposed.to_request():
                raise ValueError("同一客户端订单编号对应不同请求")
            return existing
        if proposed.instrument_id != self.instrument.instrument_id:
            raise ValueError("Broker 收到不匹配的交易品种")
        if proposed.order_type not in {OrderType.LIMIT, OrderType.MARKET}:
            raise ValueError("回测 Broker 只支持 LIMIT 和显式保护性 MARKET 卖单")
        if proposed.order_type is OrderType.MARKET and proposed.side is not OrderSide.SELL:
            raise ValueError("回测 MARKET 仅用于显式减仓")
        if (
            not proposed.price.is_finite()
            or not proposed.quantity.is_finite()
            or proposed.price <= 0
            or proposed.quantity <= 0
            or proposed.price % self.instrument.price_tick != 0
            or proposed.quantity % self.instrument.quantity_step != 0
        ):
            raise ValueError("回测订单价格、数量或精度非法")
        order = Order(proposed.to_request())
        order.transition(OrderState.SUBMITTED, at=approved.approved_at)
        enough = self._can_reserve(order)
        order.transition(
            OrderState.ACCEPTED if enough else OrderState.REJECTED, at=approved.approved_at
        )
        self._orders[proposed.client_order_id] = order
        if enough:
            self._match(order)
        return order

    def _can_reserve(self, order: Order) -> bool:
        request = order.request
        same_side = [item for item in self.get_open_orders() if item.request.side is request.side]
        if request.side is OrderSide.BUY:
            reserved = sum(
                (item.request.quantity - item.filled_quantity)
                * item.request.price
                * (Decimal("1") + self.fee_rate)
                for item in same_side
            )
            needed = request.quantity * request.price * (Decimal("1") + self.fee_rate)
            return needed + reserved <= self.portfolio.balances.get(
                self.instrument.quote_currency, Decimal("0")
            )
        reserved_quantity = sum(item.request.quantity - item.filled_quantity for item in same_side)
        return request.quantity + reserved_quantity <= self.portfolio.positions.get(
            self.instrument.instrument_id, Decimal("0")
        )

    def _match(self, order: Order) -> bool:
        candle, at = self._candle, self._at
        if (
            candle is None
            or at is None
            or not order.is_open
            or order.request.created_at > at
            or self._volume_remaining <= 0
        ):
            return False
        request = order.request
        if request.order_type is OrderType.MARKET:
            reference = request.price if self._intrabar else candle.open
        elif self._intrabar:
            touched = (
                candle.low <= request.price
                if request.side is OrderSide.BUY
                else candle.high >= request.price
            )
            if not touched:
                return False
            reference = request.price
        else:
            marketable = (
                candle.open <= request.price
                if request.side is OrderSide.BUY
                else candle.open >= request.price
            )
            if not marketable:
                return False
            reference = candle.open
        multiplier = (
            Decimal("1") + self.slippage_rate
            if request.side is OrderSide.BUY
            else Decimal("1") - self.slippage_rate
        )
        rounding = ROUND_CEILING if request.side is OrderSide.BUY else ROUND_DOWN
        price = (reference * multiplier / self.instrument.price_tick).to_integral_value(
            rounding=rounding
        ) * self.instrument.price_tick
        if request.order_type is OrderType.LIMIT:
            price = (
                min(price, request.price)
                if request.side is OrderSide.BUY
                else max(price, request.price)
            )
        if price <= 0:
            raise ValueError("回测成交价格低于最小价格精度")
        quantity = self._quantity(
            min(request.quantity - order.filled_quantity, self._volume_remaining)
        )
        if quantity <= 0:
            return False
        self._fill(order, quantity, reference, price, at)
        self._volume_remaining -= quantity
        return True

    def _fill(
        self, order: Order, quantity: Decimal, reference: Decimal, price: Decimal, at: datetime
    ) -> None:
        request = order.request
        notional, fee = price * quantity, price * quantity * self.fee_rate
        instrument_id = self.instrument.instrument_id
        quote, base = self.instrument.quote_currency, self.instrument.base_currency
        position = self.portfolio.positions.get(instrument_id, Decimal("0"))
        exit_reason = None
        if request.side is OrderSide.BUY:
            if notional + fee > self.portfolio.balances.get(quote, Decimal("0")):
                raise ValueError("回测成交会导致计价币余额为负")
            if position == 0:
                self._entered_at = at
            self.portfolio.balances[quote] -= notional + fee
            self.portfolio.balances[base] = (
                self.portfolio.balances.get(base, Decimal("0")) + quantity
            )
            self.portfolio.positions[instrument_id] = position + quantity
            self._position_notional += notional
            self.portfolio.average_entry_prices[instrument_id] = self._position_notional / (
                position + quantity
            )
            self.portfolio.realized_pnl -= fee
            self._entry_quantity += quantity
            self._entry_notional += notional
            self._entry_fees += fee
        else:
            if quantity > position:
                raise ValueError("回测成交会导致基础币持仓为负")
            allocated = self._position_notional * quantity / position
            self.portfolio.realized_pnl += notional - allocated - fee
            self.portfolio.balances[quote] = (
                self.portfolio.balances.get(quote, Decimal("0")) + notional - fee
            )
            self.portfolio.balances[base] = (
                self.portfolio.balances.get(base, Decimal("0")) - quantity
            )
            self.portfolio.positions[instrument_id] = position - quantity
            self._position_notional -= allocated
            self._exit_notional += notional
            self._exit_fees += fee
            exit_reason = self._exit_reasons.get(request.signal_id, "strategy_exit")
            self._trade_exit_reasons.add(exit_reason)
            if position == quantity:
                self.portfolio.average_entry_prices.pop(instrument_id, None)
                self._close_trade(order, at)
            else:
                self.portfolio.average_entry_prices[instrument_id] = self._position_notional / (
                    position - quantity
                )
        total_quantity = order.filled_quantity + quantity
        order.average_price = (
            (order.average_price or Decimal("0")) * order.filled_quantity + price * quantity
        ) / total_quantity
        order.filled_quantity = total_quantity
        order.transition(
            OrderState.FILLED
            if total_quantity == request.quantity
            else OrderState.PARTIALLY_FILLED,
            at=at,
        )
        order.updated_at = at
        self.fills.append(
            Fill(
                request.run_id,
                request.strategy_name,
                instrument_id,
                at,
                request.side,
                quantity,
                reference,
                price,
                notional,
                fee,
                abs(price - reference) * quantity,
                request.signal_id,
                request.client_order_id,
                exit_reason,
            )
        )

    def _close_trade(self, order: Order, at: datetime) -> None:
        assert self._entered_at is not None
        pnl = self._exit_notional - self._entry_notional - self._entry_fees - self._exit_fees
        reason = (
            next(iter(self._trade_exit_reasons)) if len(self._trade_exit_reasons) == 1 else "mixed"
        )
        self.closed_trades.append(
            ClosedTrade(
                order.request.run_id,
                order.request.strategy_name,
                self.instrument.instrument_id,
                self._entered_at,
                at,
                self._entry_quantity,
                self._entry_notional,
                self._entry_fees,
                self._exit_notional,
                self._exit_fees,
                pnl,
                Decimal(str((at - self._entered_at).total_seconds())) / Decimal("3600"),
                reason,
            )
        )
        self.closed_trade_pnls.append(pnl)
        # A completed cycle has exact fill totals. Do not carry prorated-cost
        # division rounding into later cycles after inventory returns to zero.
        self.portfolio.realized_pnl = sum(self.closed_trade_pnls, start=Decimal("0"))
        self._entered_at = None
        self._position_notional = self._entry_quantity = self._entry_notional = Decimal("0")
        self._entry_fees = self._exit_notional = self._exit_fees = Decimal("0")
        self._trade_exit_reasons.clear()

    def _quantity(self, value: Decimal) -> Decimal:
        return (value / self.instrument.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.instrument.quantity_step

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        quantity = self.portfolio.positions.get(self.instrument.instrument_id, Decimal("0"))
        return quantity * mark_price - self._position_notional

    def cancel_order(self, instrument_id: str, order_id: str) -> Order:
        order = self.get_order(instrument_id, order_id)
        if order.is_open:
            order.transition(OrderState.CANCEL_PENDING, at=self._at or order.request.created_at)
            order.transition(OrderState.CANCELLED, at=self._at or order.request.created_at)
        return order

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
