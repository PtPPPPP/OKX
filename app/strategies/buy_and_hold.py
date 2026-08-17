from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order
from app.domain.signal import Signal, SignalAction


class BuyAndHoldParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuyAndHoldStrategy:
    name = "buy_and_hold"
    description = "首次获得已收盘 K 线时买入，之后持有"
    required_history = 1
    supported_market_types = frozenset({InstrumentType.SPOT})

    def __init__(self, parameters: BuyAndHoldParameters) -> None:
        self.parameters = parameters
        self._entered = False

    def on_start(self, context: StrategyContext) -> None:
        self._entered = False

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        action = SignalAction.HOLD
        reason = "继续持有"
        if bar.confirmed and not self._entered:
            action = SignalAction.BUY
            reason = "买入并持有"
            self._entered = True
        elif not bar.confirmed:
            reason = "最新 K 线尚未收盘"
        return [
            Signal(
                signal_id=uuid4().hex,
                strategy_name=self.name,
                instrument_id=context.instrument.instrument_id,
                action=action,
                timestamp=context.now,
                reason=reason,
                confidence=Decimal("1") if action is SignalAction.BUY else Decimal("0"),
                metadata={
                    "candle_timestamp": bar.timestamp.isoformat(),
                    "candle_confirmed": bar.confirmed,
                },
            )
        ]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        return None

    def on_stop(self, context: StrategyContext) -> None:
        return None
