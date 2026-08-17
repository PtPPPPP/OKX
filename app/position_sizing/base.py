from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.domain.context import MarketSnapshot
from app.domain.market import Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class PositionSizeDecision:
    quantity: Decimal
    notional: Decimal
    reason: str


class PositionSizer(Protocol):
    @property
    def name(self) -> str: ...

    def calculate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        instrument: Instrument,
    ) -> PositionSizeDecision: ...
