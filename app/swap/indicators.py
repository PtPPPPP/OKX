from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from app.swap.domain import OpenInterestPoint, SwapCandle


@dataclass(frozen=True, slots=True)
class MACDValue:
    macd_line: Decimal
    signal_line: Decimal
    histogram: Decimal
    histogram_delta: Decimal | None
    bullish_cross: bool
    bearish_cross: bool
    above_zero: bool
    below_zero: bool
    ready: bool


def macd(
    candles: list[SwapCandle], fast: int = 12, slow: int = 26, signal: int = 9
) -> MACDValue | None:
    if fast <= 0 or slow <= fast or signal <= 0 or len(candles) < slow + signal:
        return None
    closes = [item.close for item in candles]
    fast_values = _ema(closes, fast)
    slow_values = _ema(closes, slow)
    offset = slow - fast
    lines = [fast_values[index + offset] - slow_values[index] for index in range(len(slow_values))]
    signals = _ema(lines, signal)
    current = lines[-1]
    current_signal = signals[-1]
    histogram = current - current_signal
    previous_histogram = lines[-2] - signals[-2] if len(signals) >= 2 else None
    previous_line = lines[-2]
    previous_signal = signals[-2] if len(signals) >= 2 else current_signal
    return MACDValue(
        current,
        current_signal,
        histogram,
        None if previous_histogram is None else histogram - previous_histogram,
        previous_line <= previous_signal and current > current_signal,
        previous_line >= previous_signal and current < current_signal,
        current > 0,
        current < 0,
        True,
    )


def _ema(values: list[Decimal], period: int) -> list[Decimal]:
    alpha = Decimal("2") / Decimal(period + 1)
    result = [sum(values[:period], Decimal("0")) / Decimal(period)]
    for value in values[period:]:
        result.append((value - result[-1]) * alpha + result[-1])
    return result


def anchored_vwap(candles: list[SwapCandle]) -> Decimal | None:
    if not candles or any(item.base_volume <= 0 for item in candles):
        return None
    session = candles[-1].close_time.date()
    selected = [item for item in candles if item.close_time.date() == session]
    volume = sum((item.base_volume for item in selected), Decimal("0"))
    if volume <= 0:
        return None
    return (
        sum(
            (
                (item.high + item.low + item.close) / Decimal("3") * item.base_volume
                for item in selected
            ),
            Decimal("0"),
        )
        / volume
    )


def atr(candles: list[SwapCandle], period: int = 14) -> Decimal | None:
    if len(candles) < period + 1:
        return None
    sample = candles[-(period + 1) :]
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(sample)
    ]
    return sum(ranges, Decimal("0")) / Decimal(period)


def relative_volume(candles: list[SwapCandle], period: int = 20) -> Decimal | None:
    if len(candles) < period:
        return None
    volumes = [item.base_volume for item in candles[-period:]]
    baseline = sum(volumes[:-1], Decimal("0")) / Decimal(period - 1)
    return volumes[-1] / baseline if baseline > 0 else None


@dataclass(frozen=True, slots=True)
class OIValue:
    change: Decimal
    change_pct: Decimal
    z_score: Decimal | None
    price_oi_context: str


def oi_metrics(
    points: list[OpenInterestPoint], close_change: Decimal, period: int = 5
) -> OIValue | None:
    if len(points) < period or any(
        item.instrument_id != points[-1].instrument_id for item in points
    ):
        return None
    values = [item.open_interest_contracts for item in points[-period:]]
    change = values[-1] - values[-2]
    change_pct = change / values[-2] if values[-2] else Decimal("0")
    deviations = [value - sum(values, Decimal("0")) / Decimal(len(values)) for value in values]
    variance = sum((value * value for value in deviations), Decimal("0")) / Decimal(len(values))
    z_score = (
        (values[-1] - sum(values, Decimal("0")) / Decimal(len(values))) / variance.sqrt()
        if variance > 0
        else None
    )
    return OIValue(
        change,
        change_pct,
        z_score,
        f"price_{'up' if close_change >= 0 else 'down'}_oi_{'up' if change >= 0 else 'down'}",
    )
