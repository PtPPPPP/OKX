from __future__ import annotations

from typing import Any

import pytest

from backtest.market_information_features import (
    _asof,
    _causal_bucket,
    _lag_change,
    _market_context,
    _percentile,
)
from backtest.market_information_research import (
    _episodes,
    _forward,
    _ranks,
    _spearman,
    run_information_study,
)


def test_asof_never_selects_future() -> None:
    rows = [[100], [200]]
    assert _asof(rows, [100, 200], 199) == ([100], 0)


def test_asof_before_history_is_missing() -> None:
    assert _asof([[100]], [100], 99) == (None, None)


def test_causal_bucket_ignores_future_tail() -> None:
    values = [float(index) for index in range(10)]
    assert _causal_bucket(values, ("a", "b", "c", "d", "e"), 10) == "e"


def test_percentile_requires_full_window() -> None:
    assert _percentile([1.0, 2.0], 3) is None


def test_lag_change_preserves_hourly_gap() -> None:
    rows: list[dict[str, Any]] = [{"open_interest_btc": 100.0}, {"open_interest_btc": None}]
    assert _lag_change(110.0, rows, 1) is None
    assert _lag_change(110.0, rows, 2) == pytest.approx(0.1)


def test_episode_deduplicates_continuous_state() -> None:
    rows = [{"state": value} for value in ("a", "a", "b", "a", "a")]
    assert _episodes(rows, "state", "a") == [0, 3]


def test_forward_return_uses_only_later_prices() -> None:
    rows = [{"spot_close": value} for value in (100, 110, 90)]
    values, mfe, mae = _forward(rows, [0], 2)
    assert values == pytest.approx([-0.1])
    assert mfe == pytest.approx([0.1])
    assert mae == pytest.approx([-0.1])


def test_tied_ranks_are_averaged() -> None:
    assert _ranks([1.0, 1.0, 3.0]) == [1.5, 1.5, 3.0]


def test_spearman_monotonic_is_one() -> None:
    assert _spearman([(1, 2), (2, 4), (3, 6)]) == 1.0


def test_market_context_is_train_only() -> None:
    class Candle:
        def __init__(self, close: float) -> None:
            self.close = close

    rows = [Candle(100.0)] * 720 + [Candle(103.0)]
    assert _market_context(rows, 720) == "bull"


def test_study_never_emits_more_than_three_hypotheses() -> None:
    rows = _sample_rows(900)
    study = run_information_study(rows)
    assert len(study["phase_b_hypotheses"]) <= 3


def test_study_is_descriptive_only() -> None:
    study = run_information_study(_sample_rows(900))
    assert study["strategy_generated"] is False
    assert study["multiple_testing_risk"] is True


def test_study_accepts_history_before_oi_coverage() -> None:
    rows = _sample_rows(900)
    rows[0]["oi_change_1h"] = None
    rows[0]["oi_pct_change_1h"] = None
    rows[0]["open_interest_btc"] = None
    assert run_information_study(rows)["strategy_generated"] is False


def _sample_rows(count: int) -> list[dict[str, Any]]:
    states = ("negative", "neutral", "positive")
    return [
        {
            "timestamp": f"2025-{index % 12 + 1:02d}-01T00:00:00+00:00",
            "spot_close": 100 + index * 0.01,
            "funding_state": states[(index // 10) % 3],
            "basis_state": states[(index // 11) % 3],
            "funding_rate": float((index % 9) - 4),
            "basis_pct": float((index % 7) - 3),
            "open_interest_btc": 1000 + index,
            "oi_change_1h": 1.0 if index % 4 else -1.0,
            "oi_pct_change_1h": 0.001 if index % 4 else -0.001,
            "price_direction": "up" if index % 2 else "down",
            "price_oi_quadrant": f"price_{'up' if index % 2 else 'down'}_oi_{'up' if index % 4 else 'down'}",
            "volume": 10 + index % 10,
            "spot_return_1h": 0.001,
            "volatility_context": "normal",
            "market_context": "sideways",
        }
        for index in range(count)
    ]
