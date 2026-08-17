from __future__ import annotations

from typing import Protocol

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order
from app.domain.signal import Signal


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def required_history(self) -> int: ...

    @property
    def supported_market_types(self) -> frozenset[InstrumentType]: ...

    def on_start(self, context: StrategyContext) -> None: ...

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]: ...

    def on_order_update(self, context: StrategyContext, order: Order) -> None: ...

    def on_stop(self, context: StrategyContext) -> None: ...
