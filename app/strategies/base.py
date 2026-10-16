from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order
from app.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class ProtectiveExitLevels:
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    take_profit_reason: str = "take_profit"


@runtime_checkable
class ProtectiveExitProvider(Protocol):
    def protective_exit_levels(self, entry_price: Decimal) -> ProtectiveExitLevels: ...


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
