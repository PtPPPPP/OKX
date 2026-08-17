from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class InstrumentType(StrEnum):
    SPOT = "spot"
    MARGIN = "margin"
    SWAP = "swap"
    FUTURES = "futures"
    OPTION = "option"


class TradeMode(StrEnum):
    CASH = "cash"
    CROSS = "cross"
    ISOLATED = "isolated"
    SPOT_ISOLATED = "spot_isolated"


class InstrumentStatus(StrEnum):
    LIVE = "live"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    confirmed: bool


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    base_currency: str
    quote_currency: str
    instrument_type: InstrumentType
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    status: InstrumentStatus
    contract_value: Decimal | None = None
    settlement_currency: str | None = None

    @property
    def tradable(self) -> bool:
        return self.instrument_type is InstrumentType.SPOT and self.status is InstrumentStatus.LIVE
