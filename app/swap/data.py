from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.swap.domain import ContractSpecification, OpenInterestPoint, SwapCandle


@dataclass(frozen=True, slots=True)
class MultiTimeframeMarketBundle:
    decision_time: datetime
    execution_candle: SwapCandle
    candles_5m: tuple[SwapCandle, ...]
    candles_15m: tuple[SwapCandle, ...]
    candles_1h: tuple[SwapCandle, ...]
    oi_history: tuple[OpenInterestPoint, ...]
    specification: ContractSpecification
    rejection_reasons: tuple[str, ...] = ()

    @property
    def tradable(self) -> bool:
        return not self.rejection_reasons


def build_bundle(
    decision: SwapCandle,
    candles_5m: list[SwapCandle],
    candles_15m: list[SwapCandle],
    candles_1h: list[SwapCandle],
    oi: list[OpenInterestPoint],
    specification: ContractSpecification,
    maximum_oi_staleness: timedelta = timedelta(minutes=15),
) -> MultiTimeframeMarketBundle:
    reasons: list[str] = []
    decision_time = decision.close_time
    five = [item for item in candles_5m if item.close_time <= decision_time]
    fifteen = [item for item in candles_15m if item.close_time <= decision_time]
    hourly = [item for item in candles_1h if item.close_time <= decision_time]
    if (
        decision not in five
        or _invalid_candles(five)
        or _invalid_candles(fifteen)
        or _invalid_candles(hourly)
    ):
        reasons.append("timeframe_alignment_failure")
    if not fifteen or not hourly:
        reasons.append("higher_timeframe_missing")
    eligible_oi = [item for item in oi if item.timestamp <= decision_time]
    if not eligible_oi or decision_time - eligible_oi[-1].timestamp > maximum_oi_staleness:
        reasons.append("oi_missing_or_stale")
    if decision.quote_volume is None:
        reasons.append("quote_volume_missing")
    return MultiTimeframeMarketBundle(
        decision_time,
        decision,
        tuple(five),
        tuple(fifteen),
        tuple(hourly),
        tuple(eligible_oi),
        specification,
        tuple(reasons),
    )


def _invalid_candles(candles: list[SwapCandle]) -> bool:
    seen: set[datetime] = set()
    for item in candles:
        if not item.confirmed or item.close_time in seen:
            return True
        seen.add(item.close_time)
    return False
