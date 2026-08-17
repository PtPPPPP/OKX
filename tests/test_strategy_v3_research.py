from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.market import Candle
from backtest.strategy_v2_candidates import CandidateVariant, EntryEpisode
from backtest.strategy_v2_research import _random_analysis, _walk_forward


def _candles(count: int) -> list[Candle]:
    start = datetime(2023, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("100") + Decimal(index) / Decimal("100"),
            high=Decimal("102") + Decimal(index) / Decimal("100"),
            low=Decimal("99") + Decimal(index) / Decimal("100"),
            close=Decimal("101") + Decimal(index) / Decimal("100"),
            volume=Decimal("10"),
            confirmed=True,
        )
        for index in range(count)
    ]


def _episode(candles: list[Candle], index: int) -> EntryEpisode:
    values: dict[int, float | None] = {horizon: 0.001 for horizon in (1, 3, 6, 12, 24, 48, 72)}
    return EntryEpisode(
        f"v3-{index}",
        "htf_breakout",
        "htf_breakout_20",
        index,
        index,
        candles[index].timestamp.isoformat(),
        index + 1,
        candles[index + 1].timestamp.isoformat(),
        float(candles[index + 1].open),
        1,
        True,
        "signal_ended",
        "bull",
        "normal",
        False,
        values,
        values,
        {horizon: -0.001 for horizon in values},
    )


def test_v3_random_benchmark_is_deterministic() -> None:
    candles = _candles(2_000)
    episodes = tuple(_episode(candles, index) for index in range(200, 1_500, 50))
    variant = CandidateVariant("htf_breakout", "htf_breakout_20", "r", "e", "f", {}, True)
    assert _random_analysis(candles, episodes, variant) == _random_analysis(
        candles, episodes, variant
    )


def test_v3_walk_forward_is_chronological() -> None:
    candles = _candles(24 * 365 * 3)
    episodes = tuple(_episode(candles, index) for index in range(200, len(candles) - 100, 100))
    rows = _walk_forward(candles, episodes, 24, int(len(candles) * 0.8))
    assert rows
    assert all(
        row["train_start"] < row["train_end"] <= row["test_start"] < row["test_end"] for row in rows
    )
