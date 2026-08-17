"""Causal higher-timeframe and relative-volume features for Strategy Research V3."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from statistics import median

from app.domain.market import Candle

ONE_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class HigherTimeframeCandle:
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_bar_count: int


def aggregate_completed(candles: list[Candle], *, hours: int) -> tuple[HigherTimeframeCandle, ...]:
    if hours not in {4, 24}:
        raise ValueError("supported higher timeframes are 4H and 1D")
    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        stamp = candle.timestamp.astimezone(UTC)
        bucket_hour = stamp.hour - stamp.hour % hours if hours < 24 else 0
        start = stamp.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(start, []).append(candle)
    result: list[HigherTimeframeCandle] = []
    for start, items in sorted(buckets.items()):
        items.sort(key=lambda item: item.timestamp)
        if len(items) != hours or any(
            later.timestamp - earlier.timestamp != ONE_HOUR for earlier, later in pairwise(items)
        ):
            continue
        if items[0].timestamp.astimezone(UTC) != start:
            continue
        result.append(
            HigherTimeframeCandle(
                timeframe=f"{hours}H" if hours < 24 else "1D",
                open_time=start,
                close_time=start + timedelta(hours=hours),
                open=items[0].open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=items[-1].close,
                volume=sum((item.volume for item in items), Decimal("0")),
                source_bar_count=hours,
            )
        )
    return tuple(result)


def completed_htf_index(
    bars: tuple[HigherTimeframeCandle, ...], decision_time: datetime
) -> int | None:
    """Return the latest bar fully closed by a confirmed 1H candle close."""
    index = bisect_right(tuple(item.close_time for item in bars), decision_time) - 1
    return index if index >= 0 else None


def htf_uptrend(
    bars: tuple[HigherTimeframeCandle, ...],
    decision_time: datetime,
    *,
    fast: int,
    slow: int,
) -> bool:
    index = completed_htf_index(bars, decision_time)
    return _htf_uptrend_at(bars, index, fast=fast, slow=slow)


def causal_htf_uptrend_flags(
    candles: list[Candle],
    bars: tuple[HigherTimeframeCandle, ...],
    *,
    fast: int,
    slow: int,
) -> tuple[bool, ...]:
    """Evaluate each 1H decision with one linear pass over completed 4H bars."""
    if fast <= 0 or slow <= 0 or fast > slow:
        raise ValueError("HTF averages require 0 < fast <= slow")
    result: list[bool] = []
    completed_index = -1
    for candle in candles:
        decision_time = candle.timestamp + ONE_HOUR
        while (
            completed_index + 1 < len(bars)
            and bars[completed_index + 1].close_time <= decision_time
        ):
            completed_index += 1
        result.append(_htf_uptrend_at(bars, completed_index, fast=fast, slow=slow))
    return tuple(result)


def _htf_uptrend_at(
    bars: tuple[HigherTimeframeCandle, ...],
    index: int | None,
    *,
    fast: int,
    slow: int,
) -> bool:
    if index is None or index < 0 or index + 1 < slow:
        return False
    closes = [float(item.close) for item in bars[index - slow + 1 : index + 1]]
    fast_mean = sum(closes[-fast:]) / fast
    slow_mean = sum(closes) / slow
    return fast_mean > slow_mean and closes[-1] > slow_mean


def relative_volume(volumes: Sequence[object], index: int, *, window: int) -> float | None:
    """Current confirmed base volume divided by the median of prior confirmed bars."""
    if index < window or window <= 0:
        return None
    current = _valid_volume(volumes[index])
    reference = [_valid_volume(value) for value in volumes[index - window : index]]
    denominator = median(reference)
    if denominator <= 0:
        return None
    return float(current / denominator)


def validate_volumes(candles: list[Candle]) -> dict[str, object]:
    malformed = 0
    zero = 0
    for candle in candles:
        try:
            value = _valid_volume(candle.volume)
        except ValueError:
            malformed += 1
            continue
        zero += int(value == 0)
    return {
        "volume_field": "volume",
        "volume_semantics": "BTC base currency quantity for BTC-USDT spot",
        "volume_rows": len(candles),
        "zero_volume_rows": zero,
        "malformed_volume_rows": malformed,
        "volume_quality_pass": malformed == 0,
    }


def validate_research_partition(
    candles: list[Candle],
    *,
    research_cutoff: datetime,
    prospective_start: datetime,
) -> None:
    if research_cutoff >= prospective_start:
        raise ValueError("research cutoff must precede prospective OOS")
    if any(candle.timestamp >= prospective_start for candle in candles):
        raise ValueError("prospective data must be excluded from V3 research")
    if candles and candles[-1].timestamp > research_cutoff:
        raise ValueError("historical research data exceeds the frozen cutoff")


def _valid_volume(value: object) -> Decimal:
    if value is None:
        raise ValueError("missing volume")
    if not isinstance(value, (Decimal, float, str, tuple)):
        raise ValueError("malformed volume")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("malformed volume") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("volume must be finite and non-negative")
    return parsed
