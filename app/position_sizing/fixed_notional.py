from __future__ import annotations

from decimal import Decimal

from app.domain.context import MarketSnapshot
from app.domain.market import Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.position_sizing.base import PositionSizeDecision
from app.risk.position_sizing import quantize_down


class FixedNotionalPositionSizer:
    name = "fixed_notional"

    def __init__(self, order_notional: Decimal) -> None:
        if not order_notional.is_finite() or order_notional <= 0:
            raise ValueError("order_notional 必须大于 0")
        self.order_notional = order_notional

    def calculate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        instrument: Instrument,
    ) -> PositionSizeDecision:
        if market.price <= 0:
            raise ValueError("市场价格必须大于 0")
        if signal.action is SignalAction.BUY:
            requested = signal.suggested_notional or self.order_notional
            quantity = quantize_down(requested / market.price, instrument.quantity_step)
            return PositionSizeDecision(
                quantity=quantity,
                notional=quantity * market.price,
                reason=f"按 {instrument.quote_currency} 固定名义金额计算",
            )
        if signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            position = portfolio.available_position(
                instrument.instrument_id, instrument.base_currency
            )
            quantity = quantize_down(position, instrument.quantity_step)
            return PositionSizeDecision(
                quantity=quantity,
                notional=quantity * market.price,
                reason="卖出当前策略持仓",
            )
        return PositionSizeDecision(Decimal("0"), Decimal("0"), "hold 信号无需仓位")
