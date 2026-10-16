from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


def validate_confirmed_bar(
    candle: Candle, previous: Candle | None = None, *, interval: timedelta
) -> None:
    """Strict input contract for versioned strategies; never sort, fill or reset data."""
    stamp = candle.timestamp
    seconds = int(interval.total_seconds())
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError("candle timestamp must be aware UTC opening time")
    if seconds <= 0 or stamp.microsecond or stamp.timestamp() % seconds:
        raise ValueError("candle opening time is not aligned to the configured interval")
    if candle.confirmed is not True:
        raise ValueError("strategy accepts confirmed closed candles only")
    if any(
        not value.is_finite()
        for value in (candle.open, candle.high, candle.low, candle.close, candle.volume)
    ):
        raise ValueError("candle values must be finite")
    if (
        candle.volume < 0
        or candle.low <= 0
        or candle.high < max(candle.open, candle.close)
        or candle.low > min(candle.open, candle.close)
    ):
        raise ValueError("invalid candle OHLC/volume")
    if previous is not None:
        if stamp == previous.timestamp:
            raise ValueError("duplicate candle; state unchanged")
        if stamp < previous.timestamp:
            raise ValueError("out-of-order candle; state unchanged")
        if stamp != previous.timestamp + interval:
            raise ValueError("missing candles; restore continuity before proceeding")


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
