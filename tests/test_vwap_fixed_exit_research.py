from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.domain.market import Candle
from backtest.vwap_episode_research import Episode
from backtest.vwap_fixed_exit_research import (
    CostModel,
    TradeCandidate,
    _random_candidates,
    _random_portfolio_metrics,
    drawdown_statistics,
    holdout_performance,
    performance_metrics,
    select_episode_candidates,
    simulate_portfolio,
)


def _candles(count: int, *, future_multiplier: Decimal = Decimal("1")) -> list[Candle]:
    result: list[Candle] = []
    for index in range(count):
        price = (Decimal("100") + index) * (future_multiplier if index >= 10 else 1)
        result.append(
            Candle(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=Decimal("10"),
                confirmed=True,
            )
        )
    return result


def _episode(index: int, *, holdout: bool = False) -> Episode:
    stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    return Episode(
        episode_id=f"episode-{index}",
        start_index=index,
        end_index=index,
        start_signal_timestamp=stamp.isoformat(),
        end_signal_timestamp=stamp.isoformat(),
        first_entry_reference_timestamp=(stamp + timedelta(hours=1)).isoformat(),
        first_entry_reference_price=101.0,
        duration_bars=1,
        duration_hours=1,
        buy_signal_count=1,
        max_deviation=100,
        min_deviation=100,
        start_deviation=100,
        end_deviation=100,
        start_vwap=100,
        start_close=99,
        next_bar_open=101,
        closed=True,
        closure_reason="buy_condition_ended",
        market_regime="bull",
        volatility_regime="normal",
        temporal_slice="first_third",
        holdout=holdout,
        returns={},
        mfe={},
        mae={},
    )


def test_episode_only_next_bar_open_and_fixed_6h_exit() -> None:
    candles = _candles(12)
    selected, blocked = select_episode_candidates(candles, (_episode(0),), 6)
    trades, _ = simulate_portfolio(candles, selected, 6, CostModel.equal_split(0))

    assert blocked == 0
    assert len(trades) == 1
    assert trades[0]["entry_index"] == 1
    assert trades[0]["exit_index"] == 7
    assert trades[0]["holding_hours"] == 6
    assert trades[0]["entry_price_raw"] == float(candles[1].open)
    assert trades[0]["exit_price_raw"] == float(candles[7].open)


@pytest.mark.parametrize("horizon", [12, 24, 48])
def test_fixed_exit_off_by_one_contract(horizon: int) -> None:
    candles = _candles(horizon + 4)
    selected, _ = select_episode_candidates(candles, (_episode(1),), horizon)
    trades, _ = simulate_portfolio(candles, selected, horizon, CostModel.equal_split(0))

    assert trades[0]["entry_index"] == 2
    assert trades[0]["exit_index"] == 2 + horizon
    assert trades[0]["exit_price_raw"] == float(candles[2 + horizon].open)


def test_one_position_only_ignores_episode_signaled_before_exit() -> None:
    candles = _candles(20)
    selected, blocked = select_episode_candidates(
        candles, (_episode(0), _episode(3), _episode(6), _episode(7)), 6
    )

    assert [item.entry_index for item in selected] == [1, 8]
    assert blocked == 2


def test_cost_and_slippage_always_hurt_long_trade() -> None:
    candles = _candles(12)
    candidate = (
        TradeCandidate("e", candles[0].timestamp.isoformat(), 1, 7, "bull", "normal", False),
    )
    zero, _ = simulate_portfolio(candles, candidate, 6, CostModel.equal_split(0))
    costly, _ = simulate_portfolio(candles, candidate, 6, CostModel.equal_split(20))

    assert costly[0]["entry_price_net"] > costly[0]["entry_price_raw"]
    assert costly[0]["exit_price_net"] < costly[0]["exit_price_raw"]
    assert costly[0]["net_return"] < zero[0]["net_return"]
    assert costly[0]["fee_cost"] > 0
    assert costly[0]["slippage_cost"] > 0
    assert float(costly[0]["gross_pnl"]) - float(costly[0]["total_cost"]) == pytest.approx(
        float(costly[0]["net_pnl"])
    )


def test_equity_accounting_and_drawdown() -> None:
    candles = _candles(12)
    candidate = (
        TradeCandidate("e", candles[0].timestamp.isoformat(), 1, 7, "bull", "normal", False),
    )
    trades, equity = simulate_portfolio(candles, candidate, 6, CostModel.equal_split(10))

    assert float(trades[0]["equity_after"]) == pytest.approx(float(equity[7]["equity"]))
    assert float(trades[0]["net_pnl"]) == pytest.approx(float(trades[0]["equity_after"]) - 100_000)
    assert drawdown_statistics([100, 90, 80, 85, 101]) == pytest.approx((-0.2, 3, 3))
    metrics = performance_metrics(equity, trades, candles, horizon=6, cost_bps=10)
    assert float(metrics["top_1_trade_contribution"]) >= 1


def test_future_change_does_not_change_prior_trade() -> None:
    first = _candles(20)
    altered = _candles(20, future_multiplier=Decimal("3"))
    candidate = (
        TradeCandidate("e", first[0].timestamp.isoformat(), 1, 7, "bull", "normal", False),
    )

    original, _ = simulate_portfolio(first, candidate, 6, CostModel.equal_split(10))
    replayed, _ = simulate_portfolio(altered, candidate, 6, CostModel.equal_split(10))
    assert original == replayed


def test_random_benchmark_is_deterministic_and_non_overlapping() -> None:
    import numpy as np

    first = _random_candidates(500, 30, 6, np.random.default_rng(42))
    second = _random_candidates(500, 30, 6, np.random.default_rng(42))
    assert first == second
    assert all(later.entry_index >= earlier.exit_index for earlier, later in pairwise(first))


def test_vectorized_random_metrics_match_full_portfolio() -> None:
    candles = _candles(100)
    candidates = (
        TradeCandidate("a", "random", 2, 8, "random", "random", False),
        TradeCandidate("b", "random", 15, 21, "random", "random", False),
    )
    cost = CostModel.equal_split(10)
    trades, equity = simulate_portfolio(candles, candidates, 6, cost)
    expected = performance_metrics(equity, trades, candles, horizon=6, cost_bps=10)
    actual = _random_portfolio_metrics(candles, candidates, cost)

    assert actual[0] == pytest.approx(expected["total_return"])
    assert actual[1] == pytest.approx(expected["Sharpe"])
    assert actual[2] == pytest.approx(expected["max_drawdown"])


def test_recent_holdout_split_uses_episode_flag() -> None:
    rows = [
        {"net_return": 0.1, "holdout": False},
        {"net_return": -0.1, "holdout": True},
    ]
    result = holdout_performance(rows, 6, 10)
    assert result["holdout_trade_count"] == 1
    assert result["holdout_net_return"] == pytest.approx(-0.1)
