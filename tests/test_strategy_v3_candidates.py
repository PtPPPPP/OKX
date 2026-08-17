from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.market import Candle
from backtest.strategy_v2_candidates import CandidateVariant, build_entry_episodes
from backtest.strategy_v3_candidates import V3Variant, generate_v3_signals, load_v3_specs
from backtest.strategy_v3_features import aggregate_completed


def _candles(count: int = 500) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    for index in range(count):
        base = Decimal("100") + Decimal(index) / Decimal("20")
        close = base + (Decimal("1") if index % 17 == 0 else Decimal("0.1"))
        result.append(
            Candle(
                timestamp=start + timedelta(hours=index),
                open=base,
                high=max(base, close) + Decimal("0.5"),
                low=min(base, close) - Decimal("0.5"),
                close=close,
                volume=Decimal(10 + index % 23),
                confirmed=True,
            )
        )
    return result


def _variants() -> tuple[V3Variant, ...]:
    return load_v3_specs(Path("configs/research/strategy_v3_candidate_specs.json"))


@pytest.mark.parametrize(
    "candidate_id",
    [
        "htf_pullback_recovery",
        "htf_breakout",
        "relative_volume_breakout",
        "htf_volume_momentum",
        "volume_exhaustion_reversal",
    ],
)
def test_each_v3_candidate_is_causal(candidate_id: str) -> None:
    candles = _candles()
    bars = aggregate_completed(candles, hours=4)
    variant = next(
        item for item in _variants() if item.candidate_id == candidate_id and item.primary
    )
    baseline = generate_v3_signals(candles, bars, variant)
    assert generate_v3_signals(candles, bars, variant) == baseline
    altered = list(candles)
    for index in range(351, len(altered)):
        altered[index] = replace(
            altered[index],
            open=altered[index].open * 10,
            high=altered[index].high * 10,
            low=altered[index].low * 10,
            close=altered[index].close * 10,
            volume=altered[index].volume * 10,
        )
    altered_signals = generate_v3_signals(altered, aggregate_completed(altered, hours=4), variant)
    assert altered_signals.candidate[:351] == baseline.candidate[:351]


def test_volume_incremental_control_removes_only_volume_condition() -> None:
    candles = _candles()
    bars = aggregate_completed(candles, hours=4)
    variant = next(item for item in _variants() if item.variant_id == "volume_breakout_20")
    signals = generate_v3_signals(candles, bars, variant)
    assert signals.without_volume is not None
    assert all(
        not candidate or control
        for candidate, control in zip(signals.candidate, signals.without_volume, strict=True)
    )


def test_htf_incremental_control_removes_only_htf_condition() -> None:
    candles = _candles()
    bars = aggregate_completed(candles, hours=4)
    variant = next(item for item in _variants() if item.variant_id == "htf_breakout_20")
    signals = generate_v3_signals(candles, bars, variant)
    assert signals.without_htf is not None
    assert all(
        not candidate or control
        for candidate, control in zip(signals.candidate, signals.without_htf, strict=True)
    )


def test_v3_episode_dedup_next_open_and_replay() -> None:
    candles = _candles(30)
    variant = next(item for item in _variants() if item.primary)
    adapter = CandidateVariant(
        variant.candidate_id,
        variant.variant_id,
        "r",
        "e",
        "f",
        variant.parameters,
        True,
    )
    signals = tuple(index in {5, 6, 7, 20} for index in range(30))
    first = build_entry_episodes(candles, signals, adapter, (1, 3, 6))
    second = build_entry_episodes(candles, signals, adapter, (1, 3, 6))
    assert first == second
    assert len(first) == 2
    assert first[0].entry_index == 6
    assert first[0].entry_price == float(candles[6].open)


def test_v3_candidate_spec_is_frozen_and_bounded() -> None:
    variants = _variants()
    assert len({item.candidate_id for item in variants}) == 5
    assert len(variants) == 10
    assert all(
        sum(item.primary for item in variants if item.candidate_id == candidate) == 1
        for candidate in {item.candidate_id for item in variants}
    )
