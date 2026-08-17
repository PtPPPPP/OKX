from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.domain.market import Candle
from backtest.vwap_episode_research import Episode
from backtest.vwap_fixed_exit_research import CostModel
from backtest.vwap_walk_forward_research import (
    CANDIDATES,
    CandidateSpec,
    _passes_filter,
    add_months,
    build_walk_forward_windows,
    causal_trend,
    causal_volatility,
    concentration_diagnostics,
    evaluate_period,
    fit_volatility_thresholds,
)


def _candles(count: int) -> list[Candle]:
    start = datetime(2023, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    price = Decimal("100")
    for index in range(count):
        price *= Decimal("1.0002") if index % 19 else Decimal("0.998")
        result.append(
            Candle(
                timestamp=start + timedelta(hours=index),
                open=price,
                high=price * Decimal("1.002"),
                low=price * Decimal("0.998"),
                close=price,
                volume=Decimal("10"),
                confirmed=True,
            )
        )
    return result


def _episode(index: int) -> Episode:
    stamp = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    return Episode(
        episode_id=f"episode-{index}",
        start_index=index,
        end_index=index,
        start_signal_timestamp=stamp.isoformat(),
        end_signal_timestamp=stamp.isoformat(),
        first_entry_reference_timestamp=(stamp + timedelta(hours=1)).isoformat(),
        first_entry_reference_price=100,
        duration_bars=1,
        duration_hours=1,
        buy_signal_count=1,
        max_deviation=100,
        min_deviation=100,
        start_deviation=100,
        end_deviation=100,
        start_vwap=100,
        start_close=99,
        next_bar_open=100,
        closed=True,
        closure_reason="buy_condition_ended",
        market_regime="unused",
        volatility_regime="unused",
        temporal_slice="unused",
        holdout=False,
        returns={},
        mfe={},
        mae={},
    )


def test_chronological_split_and_train_test_non_overlap() -> None:
    candles = _candles(24 * 365 * 3)
    holdout_index = int(len(candles) * 0.8)
    windows = build_walk_forward_windows(candles, holdout_index)

    assert windows
    assert all(window.train_end_index == window.test_start_index for window in windows)
    assert all(window.train_end_index <= window.test_start_index for window in windows)
    assert all(window.test_end_index <= holdout_index for window in windows)
    assert all(
        later.test_start_index > earlier.test_start_index for earlier, later in pairwise(windows)
    )


def test_final_holdout_is_excluded_from_development_windows() -> None:
    candles = _candles(24 * 365 * 3)
    holdout_index = int(len(candles) * 0.8)
    windows = build_walk_forward_windows(candles, holdout_index)
    assert max(window.test_end_index for window in windows) <= holdout_index


def test_calendar_month_arithmetic_is_deterministic() -> None:
    assert add_months(datetime(2024, 1, 31, tzinfo=UTC), 1) == datetime(2024, 2, 29, tzinfo=UTC)
    assert add_months(datetime(2023, 1, 31, tzinfo=UTC), 1) == datetime(2023, 2, 28, tzinfo=UTC)


def test_regime_threshold_is_fit_from_train_only() -> None:
    candles = _candles(24 * 500)
    episodes = tuple(_episode(index) for index in range(200, 24 * 500, 48))
    baseline = fit_volatility_thresholds(candles, episodes, 0, 24 * 365)
    altered = list(candles)
    for index in range(24 * 365, len(altered)):
        candle = altered[index]
        altered[index] = Candle(
            timestamp=candle.timestamp,
            open=candle.open * 3,
            high=candle.high * 3,
            low=candle.low * 3,
            close=candle.close * 3,
            volume=candle.volume,
            confirmed=True,
        )
    assert fit_volatility_thresholds(altered, episodes, 0, 24 * 365) == baseline


def test_causal_regimes_ignore_future_prices() -> None:
    candles = _candles(500)
    altered = list(candles)
    for index in range(301, len(altered)):
        candle = altered[index]
        altered[index] = Candle(
            timestamp=candle.timestamp,
            open=candle.open * 10,
            high=candle.high * 10,
            low=candle.low * 10,
            close=candle.close * 10,
            volume=candle.volume,
            confirmed=True,
        )
    assert causal_volatility(candles, 300) == causal_volatility(altered, 300)
    assert causal_trend(candles, 300) == causal_trend(altered, 300)


def test_filters_use_frozen_thresholds_and_causal_trend() -> None:
    candles = _candles(500)
    episode = _episode(300)
    volatility = causal_volatility(candles, 300)
    assert volatility is not None
    normal = next(item for item in CANDIDATES if item.candidate_id == "h24_normal_vol")
    assert _passes_filter(candles, episode, normal, (volatility * 0.9, volatility * 1.1))
    assert not _passes_filter(candles, episode, normal, (volatility * 1.1, volatility * 1.2))


def test_evaluation_is_candidate_isolated_and_deterministic() -> None:
    candles = _candles(1_000)
    episodes = tuple(_episode(index) for index in (200, 230, 260, 500, 700))
    candidate = CandidateSpec("fixture", 24)
    first = evaluate_period(
        candles,
        episodes,
        candidate,
        168,
        900,
        CostModel.equal_split(10),
        (0.0, 10.0),
        period_id="fixture",
        phase="test",
    )
    second = evaluate_period(
        candles,
        episodes,
        candidate,
        168,
        900,
        CostModel.equal_split(10),
        (0.0, 10.0),
        period_id="fixture",
        phase="test",
    )
    assert first == second
    assert all(row["candidate_id"] == "fixture" for row in first.trades)
    assert all(row["round_trip_cost_bps"] == 10 for row in first.trades)


@pytest.mark.parametrize("cost_bps", [10, 20])
def test_cost_scenarios_are_explicit(cost_bps: int) -> None:
    candles = _candles(500)
    result = evaluate_period(
        candles,
        (_episode(200),),
        CandidateSpec("fixture", 24),
        168,
        400,
        CostModel.equal_split(cost_bps),
        (0.0, 10.0),
        period_id="fixture",
        phase="test",
    )
    assert result.metrics["round_trip_cost_bps"] == cost_bps


def test_top_winner_removal_is_deterministic_and_penalizes_concentration() -> None:
    trades = [
        {"trade_id": f"trade-{index}", "net_return": value}
        for index, value in enumerate((0.50, 0.20, 0.10, 0.05, 0.04, -0.10, -0.10, -0.10))
    ]
    first = concentration_diagnostics(trades, scope="fixture")
    second = concentration_diagnostics(trades, scope="fixture")
    assert first == second
    assert first[0]["total_return_after_removal"] > 0
    assert first[1]["total_return_after_removal"] < 0
