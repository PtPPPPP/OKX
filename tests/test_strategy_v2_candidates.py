from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.market import Candle
from backtest.strategy_v2_candidates import (
    CandidateVariant,
    build_entry_episodes,
    generate_signals,
    load_candidate_specs,
)


def _candles(count: int = 220) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    for index in range(count):
        base = Decimal("100") + Decimal(index) * Decimal("0.10")
        close = base + (Decimal("1") if index % 11 == 0 else Decimal("0"))
        result.append(
            Candle(
                timestamp=start + timedelta(hours=index),
                open=base,
                high=max(base, close) + Decimal("0.5"),
                low=min(base, close) - Decimal("0.5"),
                close=close,
                volume=Decimal("10"),
                confirmed=True,
            )
        )
    return result


def _variants() -> tuple[CandidateVariant, ...]:
    return load_candidate_specs(Path("configs/research/strategy_v2_candidate_specs.json"))


@pytest.mark.parametrize(
    "candidate_id",
    [
        "trend_continuation",
        "price_breakout",
        "volatility_breakout",
        "confirmed_mean_reversion",
        "momentum_pullback",
    ],
)
def test_candidate_signal_is_causal(candidate_id: str) -> None:
    candles = _candles()
    variant = next(
        item for item in _variants() if item.candidate_id == candidate_id and item.primary
    )
    baseline = generate_signals(candles, variant)
    altered = list(candles)
    for index in range(151, len(altered)):
        altered[index] = replace(
            altered[index],
            open=altered[index].open * 10,
            high=altered[index].high * 10,
            low=altered[index].low * 10,
            close=altered[index].close * 10,
        )
    assert generate_signals(altered, variant)[:151] == baseline[:151]


def test_breakout_excludes_current_bar_from_rolling_high() -> None:
    candles = _candles(40)
    variant = next(item for item in _variants() if item.variant_id == "breakout_20")
    unchanged = list(candles)
    unchanged[22] = replace(unchanged[22], close=Decimal("100"), high=Decimal("1000"))
    assert generate_signals(unchanged, variant)[22] is False
    changed = list(unchanged)
    changed[22] = replace(changed[22], close=Decimal("1000"), high=Decimal("1001"))
    assert generate_signals(changed, variant)[22] is True


def test_episode_dedup_and_next_bar_open() -> None:
    candles = _candles(20)
    variant = next(item for item in _variants() if item.variant_id == "breakout_20")
    signals = tuple(index in {5, 6, 7, 12} for index in range(20))
    episodes = build_entry_episodes(candles, signals, variant, (1, 3, 6))
    assert len(episodes) == 2
    assert episodes[0].start_index == 5
    assert episodes[0].end_index == 7
    assert episodes[0].entry_index == 6
    assert episodes[0].entry_price == float(candles[6].open)


def test_candidate_spec_is_frozen_and_bounded() -> None:
    variants = _variants()
    assert len({item.candidate_id for item in variants}) == 5
    assert len(variants) == 10
    assert all(
        sum(item.primary for item in variants if item.candidate_id == candidate) == 1
        for candidate in {item.candidate_id for item in variants}
    )
