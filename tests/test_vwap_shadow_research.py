from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.market import Candle, Instrument
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_shadow_research import replay_shadow, validate_candles


def _candles() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            start + timedelta(hours=index),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("98"),
            Decimal("10"),
            True,
        )
        for index in range(25)
    ]


def test_shadow_research_excludes_unconfirmed_and_requires_warmup(
    btc_instrument: Instrument,
) -> None:
    candles = _candles()
    candles[4] = Candle(
        candles[4].timestamp,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("98"),
        Decimal("10"),
        False,
    )
    records = replay_shadow(candles, btc_instrument, VWAPShadowParameters(vwap_window=24))
    assert records[4].action == "hold"
    assert not any(record.proposal_eligible for record in records)


def test_quality_reports_missing_candle() -> None:
    candles = _candles()
    del candles[5]
    quality = validate_candles(candles, timedelta(hours=1))
    assert quality.missing == 1


def test_replay_is_deterministic_and_uses_next_open(btc_instrument: Instrument) -> None:
    candles = _candles()
    first = replay_shadow(candles, btc_instrument, VWAPShadowParameters(vwap_window=24))
    second = replay_shadow(candles, btc_instrument, VWAPShadowParameters(vwap_window=24))
    assert first == second
    first_buy = next(record for record in first if record.proposal_eligible)
    position = next(
        index
        for index, candle in enumerate(candles)
        if candle.timestamp.isoformat() == first_buy.timestamp
    )
    assert first_buy.execution_reference_price == str(candles[position + 1].open)
