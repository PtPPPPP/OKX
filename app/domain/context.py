from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.config.settings import TradingMode
from app.domain.market import Candle, Instrument
from app.domain.position import PortfolioSnapshot
from app.runtime.clock import Clock


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    candle: Candle
    price: Decimal


@dataclass(frozen=True, slots=True)
class StrategyContext:
    run_id: str
    mode: TradingMode
    strategy_name: str
    instrument: Instrument
    bar: str
    portfolio_snapshot: PortfolioSnapshot
    market_snapshot: MarketSnapshot | None
    clock: Clock

    @property
    def now(self) -> datetime:
        return self.clock.now()
