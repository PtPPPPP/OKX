from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.market import Candle
from app.market.historical_data import BAR_INTERVALS, MarketDataError

_BASIS_POINTS = Decimal("10000")
_PRICE_QUANTUM = Decimal("0.01")
_VOLUME_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class SyntheticCandleRequest:
    count: int
    seed: int = 0
    bar_interval: str = "1h"
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    initial_price: Decimal = Decimal("30000")
    zero_volume_at: frozenset[int] = field(default_factory=frozenset)
    missing_at: frozenset[int] = field(default_factory=frozenset)
    duplicate_at: frozenset[int] = field(default_factory=frozenset)
    unconfirmed_at: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("synthetic candle count must be greater than zero")
        if self.bar_interval.lower() not in BAR_INTERVALS:
            raise MarketDataError(f"不支持的 K 线周期: {self.bar_interval}")
        if self.start.tzinfo is None:
            raise ValueError("synthetic candle start must include timezone")
        if not self.initial_price.is_finite() or self.initial_price <= 0:
            raise ValueError("synthetic initial price must be finite and positive")
        for indices in (
            self.zero_volume_at,
            self.missing_at,
            self.duplicate_at,
            self.unconfirmed_at,
        ):
            if any(index < 0 or index >= self.count for index in indices):
                raise ValueError("synthetic injection index is outside the requested range")

    def identity(self) -> dict[str, object]:
        return {
            "kind": "synthetic",
            "count": self.count,
            "seed": self.seed,
            "bar_interval": self.bar_interval.lower(),
            "start": self.start.astimezone(UTC).isoformat(),
            "initial_price": str(self.initial_price),
            "zero_volume_at": sorted(self.zero_volume_at),
            "missing_at": sorted(self.missing_at),
            "duplicate_at": sorted(self.duplicate_at),
            "unconfirmed_at": sorted(self.unconfirmed_at),
        }


def generate_synthetic_candles(request: SyntheticCandleRequest) -> list[Candle]:
    interval = BAR_INTERVALS[request.bar_interval.lower()]
    generator = random.Random(request.seed)
    candles: list[Candle] = []
    previous_close = request.initial_price

    for index in range(request.count):
        open_price = previous_close
        move_bps = Decimal(generator.randint(-180, 180))
        close_price = max(
            _PRICE_QUANTUM,
            (open_price * (_BASIS_POINTS + move_bps) / _BASIS_POINTS).quantize(_PRICE_QUANTUM),
        )
        upper_wick_bps = Decimal(generator.randint(0, 45))
        lower_wick_bps = Decimal(generator.randint(0, 45))
        high = (
            max(open_price, close_price) * (_BASIS_POINTS + upper_wick_bps) / _BASIS_POINTS
        ).quantize(_PRICE_QUANTUM)
        low = (
            min(open_price, close_price) * (_BASIS_POINTS - lower_wick_bps) / _BASIS_POINTS
        ).quantize(_PRICE_QUANTUM)
        low = max(_PRICE_QUANTUM, low)
        volume = (Decimal(generator.randint(100, 100_000)) / Decimal("100")).quantize(
            _VOLUME_QUANTUM
        )
        if index in request.zero_volume_at:
            volume = Decimal("0")
        candle = Candle(
            timestamp=request.start.astimezone(UTC) + interval * index,
            open=open_price,
            high=high,
            low=low,
            close=close_price,
            volume=volume,
            confirmed=index not in request.unconfirmed_at,
        )
        previous_close = close_price
        if index in request.missing_at:
            continue
        candles.append(candle)
        if index in request.duplicate_at:
            candles.append(candle)
    return candles
