from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market import (
    Candle,
    Instrument,
    InstrumentStatus,
    InstrumentType,
)


@pytest.fixture
def btc_instrument() -> Instrument:
    return make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")


@pytest.fixture
def eth_instrument() -> Instrument:
    return make_instrument("ETH-USDT", "ETH", "USDT", "0.0001", "0.01")


def make_instrument(
    instrument_id: str,
    base: str,
    quote: str,
    quantity_step: str,
    price_tick: str,
    *,
    status: InstrumentStatus = InstrumentStatus.LIVE,
) -> Instrument:
    return Instrument(
        instrument_id=instrument_id,
        base_currency=base,
        quote_currency=quote,
        instrument_type=InstrumentType.SPOT,
        price_tick=Decimal(price_tick),
        quantity_step=Decimal(quantity_step),
        minimum_quantity=Decimal(quantity_step),
        minimum_notional=Decimal("1"),
        status=status,
    )


def make_candles(
    closes: list[str], *, confirmed: bool = True, interval_minutes: int = 5
) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=interval_minutes * index),
                open=close,
                high=close + Decimal("1"),
                low=max(Decimal("0.01"), close - Decimal("1")),
                close=close,
                volume=Decimal("10"),
                confirmed=confirmed,
            )
        )
    return candles
