"""Strict chronological walk-forward research for frozen VWAP episode candidates."""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from math import log, prod, sqrt
from statistics import mean, median, pstdev, stdev
from typing import Any

import numpy as np

from app.domain.market import Candle
from backtest.vwap_episode_research import Episode
from backtest.vwap_fixed_exit_research import (
    HOURS_PER_YEAR,
    INITIAL_EQUITY,
    CostModel,
    TradeCandidate,
    _random_candidates,
    _random_portfolio_metrics,
    drawdown_statistics,
    performance_metrics,
    select_episode_candidates,
    simulate_portfolio,
)

TRAIN_MONTHS = 12
TEST_MONTHS = 3
STEP_MONTHS = 3
FINAL_HOLDOUT_FRACTION = 0.20
WALK_FORWARD_COSTS_BPS = (10, 20)
RANDOM_SAMPLE_COUNT = 200
RANDOM_SEED = 20260812
VOLATILITY_LOOKBACK_HOURS = 168
VOLATILITY_QUANTILES = (1 / 3, 2 / 3)
BOOTSTRAP_SAMPLES = 2_000
BOOTSTRAP_SEED = 20260813


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    candidate_id: str
    horizon_hours: int
    volatility_filter: str = "none"
    trend_filter: str = "none"
    role: str = "diagnostic"


CANDIDATES = (
    CandidateSpec("h24_unfiltered", 24, role="primary_frozen_candidate"),
    CandidateSpec("h48_unfiltered", 48, role="horizon_control"),
    CandidateSpec("h24_normal_vol", 24, volatility_filter="normal"),
    CandidateSpec("h24_exclude_high_vol", 24, volatility_filter="exclude_high"),
    CandidateSpec("h24_bull_only", 24, trend_filter="bull"),
    CandidateSpec("h24_bull_sideways", 24, trend_filter="bull_sideways"),
)


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    window_id: str
    train_start_index: int
    train_end_index: int
    test_start_index: int
    test_end_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str


@dataclass(frozen=True, slots=True)
class Evaluation:
    metrics: dict[str, Any]
    trades: tuple[dict[str, Any], ...]
    equity: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class WalkForwardStudy:
    windows: tuple[WalkForwardWindow, ...]
    window_rows: tuple[dict[str, Any], ...]
    candidate_rows: tuple[dict[str, Any], ...]
    stitched_rows: tuple[dict[str, Any], ...]
    holdout_rows: tuple[dict[str, Any], ...]
    regime_rows: tuple[dict[str, Any], ...]
    cost_rows: tuple[dict[str, Any], ...]
    benchmark_rows: tuple[dict[str, Any], ...]
    robustness_rows: tuple[dict[str, Any], ...]
    bootstrap_rows: tuple[dict[str, Any], ...]
    year_rows: tuple[dict[str, Any], ...]
    concentration_rows: tuple[dict[str, Any], ...]
    candidate_definitions: tuple[dict[str, Any], ...]
    holdout_start: str
    holdout_execution_count: int


def run_walk_forward_study(
    candles: list[Candle], episodes: tuple[Episode, ...]
) -> WalkForwardStudy:
    """Run frozen candidates on development windows, then open holdout exactly once."""
    if len(candles) < 24 * 365:
        raise ValueError("walk-forward research requires at least one year of hourly data")
    holdout_index = int(len(candles) * (1 - FINAL_HOLDOUT_FRACTION))
    holdout_start = candles[holdout_index].timestamp
    windows = build_walk_forward_windows(candles, holdout_index)
    if not windows:
        raise ValueError("dataset cannot form a complete walk-forward window")

    evaluations: dict[tuple[str, int, str], Evaluation] = {}
    window_rows: list[dict[str, Any]] = []
    benchmark_rows: list[dict[str, Any]] = []
    for window in windows:
        thresholds = fit_volatility_thresholds(
            candles,
            episodes,
            window.train_start_index,
            window.train_end_index,
        )
        for candidate in CANDIDATES:
            for cost_bps in WALK_FORWARD_COSTS_BPS:
                evaluation = evaluate_period(
                    candles,
                    episodes,
                    candidate,
                    window.test_start_index,
                    window.test_end_index,
                    CostModel.equal_split(cost_bps),
                    thresholds,
                    period_id=window.window_id,
                    phase="walk_forward_test",
                )
                evaluations[(candidate.candidate_id, cost_bps, window.window_id)] = evaluation
                row = {**asdict(window), **evaluation.metrics}
                window_rows.append(row)
                benchmark_rows.extend(
                    _benchmark_rows(
                        candidate,
                        cost_bps,
                        window,
                        evaluation,
                        evaluations.get(("h24_unfiltered", cost_bps, window.window_id)),
                    )
                )

    stitched_rows, candidate_rows = stitch_oos(evaluations, windows)
    regime_rows = regime_stability(evaluations)
    cost_rows = cost_stress(candidate_rows)
    year_rows = year_performance(evaluations)
    bootstrap_rows = bootstrap_oos(evaluations)
    concentration_rows = trade_removal_sensitivity(evaluations)
    robustness_rows = threshold_sensitivity(candles, episodes, windows)

    # The holdout is deliberately evaluated only after all development outputs exist.
    timestamps = [candle.timestamp for candle in candles]
    holdout_train_start = bisect_left(timestamps, add_months(holdout_start, -TRAIN_MONTHS))
    holdout_thresholds = fit_volatility_thresholds(
        candles, episodes, holdout_train_start, holdout_index
    )
    holdout_rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        for cost_bps in WALK_FORWARD_COSTS_BPS:
            evaluation = evaluate_period(
                candles,
                episodes,
                candidate,
                holdout_index,
                len(candles),
                CostModel.equal_split(cost_bps),
                holdout_thresholds,
                period_id="final_untouched_holdout",
                phase="final_holdout",
            )
            holdout_rows.append(evaluation.metrics)

    return WalkForwardStudy(
        windows=windows,
        window_rows=tuple(window_rows),
        candidate_rows=tuple(candidate_rows),
        stitched_rows=tuple(stitched_rows),
        holdout_rows=tuple(holdout_rows),
        regime_rows=tuple(regime_rows),
        cost_rows=tuple(cost_rows),
        benchmark_rows=tuple(benchmark_rows),
        robustness_rows=tuple(robustness_rows),
        bootstrap_rows=tuple(bootstrap_rows),
        year_rows=tuple(year_rows),
        concentration_rows=tuple(concentration_rows),
        candidate_definitions=tuple(asdict(candidate) for candidate in CANDIDATES),
        holdout_start=holdout_start.astimezone(UTC).isoformat(),
        holdout_execution_count=1,
    )


def build_walk_forward_windows(
    candles: list[Candle], holdout_index: int
) -> tuple[WalkForwardWindow, ...]:
    timestamps = [candle.timestamp for candle in candles]
    development_end = timestamps[holdout_index]
    train_start = timestamps[0]
    result: list[WalkForwardWindow] = []
    window_number = 1
    while True:
        train_end = add_months(train_start, TRAIN_MONTHS)
        test_end = add_months(train_end, TEST_MONTHS)
        if test_end > development_end:
            break
        train_start_index = bisect_left(timestamps, train_start)
        train_end_index = bisect_left(timestamps, train_end)
        test_end_index = bisect_left(timestamps, test_end)
        if train_end_index > train_start_index and test_end_index > train_end_index:
            result.append(
                WalkForwardWindow(
                    window_id=f"WF{window_number:02d}",
                    train_start_index=train_start_index,
                    train_end_index=train_end_index,
                    test_start_index=train_end_index,
                    test_end_index=test_end_index,
                    train_start=timestamps[train_start_index].astimezone(UTC).isoformat(),
                    train_end=timestamps[train_end_index].astimezone(UTC).isoformat(),
                    test_start=timestamps[train_end_index].astimezone(UTC).isoformat(),
                    test_end=timestamps[test_end_index].astimezone(UTC).isoformat(),
                )
            )
            window_number += 1
        train_start = add_months(train_start, STEP_MONTHS)
    return tuple(result)


def add_months(value: datetime, months: int) -> datetime:
    absolute = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(absolute, 12)
    month = month_zero + 1
    days = (31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return value.replace(year=year, month=month, day=min(value.day, days[month - 1]))


def fit_volatility_thresholds(
    candles: list[Candle], episodes: tuple[Episode, ...], start: int, end: int
) -> tuple[float, float]:
    values = [
        value
        for episode in episodes
        if start <= episode.start_index < end
        and (value := causal_volatility(candles, episode.start_index)) is not None
    ]
    if len(values) < 30:
        raise ValueError("train window has insufficient causal volatility observations")
    low, high = np.quantile(values, VOLATILITY_QUANTILES)
    return float(low), float(high)


def causal_volatility(candles: list[Candle], index: int) -> float | None:
    start = max(1, index - VOLATILITY_LOOKBACK_HOURS + 1)
    returns = [
        log(float(candles[position].close) / float(candles[position - 1].close))
        for position in range(start, index + 1)
    ]
    if len(returns) < VOLATILITY_LOOKBACK_HOURS:
        return None
    return stdev(returns) * sqrt(24 * 365)


def causal_trend(candles: list[Candle], index: int) -> str:
    start = max(0, index - VOLATILITY_LOOKBACK_HOURS + 1)
    closes = [float(candle.close) for candle in candles[start : index + 1]]
    if len(closes) < VOLATILITY_LOOKBACK_HOURS:
        return "insufficient_history"
    ratio = closes[-1] / mean(closes) - 1
    return "bull" if ratio > 0.03 else "bear" if ratio < -0.03 else "sideways"


def evaluate_period(
    candles: list[Candle],
    episodes: tuple[Episode, ...],
    candidate: CandidateSpec,
    start: int,
    end: int,
    cost: CostModel,
    thresholds: tuple[float, float],
    *,
    period_id: str,
    phase: str,
) -> Evaluation:
    eligible = tuple(
        episode
        for episode in episodes
        if start <= episode.start_index < end
        and episode.start_index + 1 + candidate.horizon_hours < end
        and _passes_filter(candles, episode, candidate, thresholds)
    )
    selected, blocked = select_episode_candidates(candles, eligible, candidate.horizon_hours)
    local = tuple(
        TradeCandidate(
            item.episode_id,
            item.signal_timestamp,
            item.entry_index - start,
            item.exit_index - start,
            causal_trend(candles, item.entry_index - 1),
            _volatility_label(causal_volatility(candles, item.entry_index - 1), thresholds),
            phase == "final_holdout",
        )
        for item in selected
    )
    segment = candles[start:end]
    trades, equity = simulate_portfolio(segment, local, candidate.horizon_hours, cost)
    metrics = performance_metrics(
        equity,
        trades,
        segment,
        horizon=candidate.horizon_hours,
        cost_bps=cost.round_trip_cost_bps,
    )
    random_percentile = _random_percentile(
        segment,
        len(local),
        candidate.horizon_hours,
        cost,
        float(metrics["total_return"]),
        f"{period_id}|{candidate.candidate_id}|{cost.round_trip_cost_bps}",
    )
    metrics.update(
        {
            "period_id": period_id,
            "phase": phase,
            "candidate_id": candidate.candidate_id,
            "candidate_role": candidate.role,
            "volatility_filter": candidate.volatility_filter,
            "trend_filter": candidate.trend_filter,
            "train_volatility_low": thresholds[0],
            "train_volatility_high": thresholds[1],
            "eligible_episode_count": len(eligible),
            "signals_blocked_by_open_position": blocked,
            "benchmark_return": float(segment[-1].open) / float(segment[0].open) - 1,
            "random_entry_percentile": random_percentile,
            "CAGR_if_meaningful": metrics["CAGR"],
            "short_window_annualization_warning": phase == "walk_forward_test",
        }
    )
    for row in trades:
        row.update(
            {
                "candidate_id": candidate.candidate_id,
                "period_id": period_id,
                "phase": phase,
            }
        )
    return Evaluation(metrics, tuple(trades), tuple(equity))


def _passes_filter(
    candles: list[Candle],
    episode: Episode,
    candidate: CandidateSpec,
    thresholds: tuple[float, float],
) -> bool:
    volatility = causal_volatility(candles, episode.start_index)
    trend = causal_trend(candles, episode.start_index)
    low, high = thresholds
    if candidate.volatility_filter == "normal" and (
        volatility is None or not low <= volatility <= high
    ):
        return False
    if candidate.volatility_filter == "exclude_high" and (volatility is None or volatility > high):
        return False
    if candidate.trend_filter == "bull" and trend != "bull":
        return False
    return not (candidate.trend_filter == "bull_sideways" and trend not in {"bull", "sideways"})


def stitch_oos(
    evaluations: dict[tuple[str, int, str], Evaluation],
    windows: tuple[WalkForwardWindow, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stitched_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        for cost_bps in WALK_FORWARD_COSTS_BPS:
            equity_level = INITIAL_EQUITY
            peak = INITIAL_EQUITY
            hourly_returns: list[float] = []
            trades: list[dict[str, Any]] = []
            window_metrics: list[dict[str, Any]] = []
            for window in windows:
                evaluation = evaluations[(candidate.candidate_id, cost_bps, window.window_id)]
                window_metrics.append(evaluation.metrics)
                trades.extend(evaluation.trades)
                local_equity = [float(row["equity"]) for row in evaluation.equity]
                scale = equity_level / INITIAL_EQUITY
                previous = equity_level
                for row, local in zip(evaluation.equity, local_equity, strict=True):
                    equity_level = local * scale
                    hourly_return = equity_level / previous - 1 if previous else 0.0
                    hourly_returns.append(hourly_return)
                    previous = equity_level
                    peak = max(peak, equity_level)
                    stitched_rows.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "round_trip_cost_bps": cost_bps,
                            "window_id": window.window_id,
                            "timestamp": row["timestamp"],
                            "equity": equity_level,
                            "drawdown": equity_level / peak - 1,
                        }
                    )
            candidate_rows.append(
                _aggregate_candidate(
                    candidate, cost_bps, window_metrics, trades, hourly_returns, equity_level
                )
            )
    _add_rolling_metrics(stitched_rows)
    return stitched_rows, candidate_rows


def _aggregate_candidate(
    candidate: CandidateSpec,
    cost_bps: int,
    windows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    hourly_returns: list[float],
    final_equity: float,
) -> dict[str, Any]:
    trade_returns = [float(row["net_return"]) for row in trades]
    losses = [value for value in trade_returns if value < 0]
    drawdowns = [float(row["max_drawdown"]) for row in windows]
    window_returns = [float(row["total_return"]) for row in windows]
    sharpes = [float(row["Sharpe"]) for row in windows if row["Sharpe"] is not None]
    factors = [float(row["profit_factor"]) for row in windows if row["profit_factor"] is not None]
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_role": candidate.role,
        "horizon_hours": candidate.horizon_hours,
        "volatility_filter": candidate.volatility_filter,
        "trend_filter": candidate.trend_filter,
        "round_trip_cost_bps": cost_bps,
        "test_windows_total": len(windows),
        "positive_test_windows": sum(value > 0 for value in window_returns),
        "negative_test_windows": sum(value < 0 for value in window_returns),
        "positive_window_ratio": sum(value > 0 for value in window_returns) / len(windows),
        "median_test_return": median(window_returns),
        "median_profit_factor": median(factors) if factors else None,
        "median_sharpe": median(sharpes) if sharpes else None,
        "worst_test_return": min(window_returns),
        "worst_max_drawdown": min(drawdowns),
        "stitched_oos_total_return": final_equity / INITIAL_EQUITY - 1,
        "stitched_oos_max_drawdown": _drawdown_from_returns(hourly_returns),
        "stitched_oos_sharpe": _sharpe(hourly_returns),
        "stitched_oos_profit_factor": (
            sum(max(value, 0) for value in trade_returns) / abs(sum(losses)) if losses else None
        ),
        "stitched_oos_win_rate": (
            sum(value > 0 for value in trade_returns) / len(trade_returns)
            if trade_returns
            else None
        ),
        "stitched_oos_trade_count": len(trade_returns),
        "average_trade": mean(trade_returns) if trade_returns else None,
    }


def regime_stability(evaluations: dict[tuple[str, int, str], Evaluation]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        trades = [
            trade
            for (candidate_id, cost, _), evaluation in evaluations.items()
            if candidate_id == candidate.candidate_id and cost == 10
            for trade in evaluation.trades
        ]
        for dimension in ("market_regime", "volatility_regime"):
            groups = sorted({str(row[dimension]) for row in trades})
            for group in groups:
                values = [float(row["net_return"]) for row in trades if row[dimension] == group]
                losses = [value for value in values if value < 0]
                rows.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "dimension": dimension,
                        "group": group,
                        "trade_count": len(values),
                        "net_return": prod(1 + value for value in values) - 1,
                        "win_rate": sum(value > 0 for value in values) / len(values),
                        "profit_factor": (
                            sum(max(value, 0) for value in values) / abs(sum(losses))
                            if losses
                            else None
                        ),
                        "average_trade": mean(values),
                    }
                )
    return rows


def cost_stress(candidate_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": row["candidate_id"],
            "round_trip_cost_bps": row["round_trip_cost_bps"],
            "stitched_oos_total_return": row["stitched_oos_total_return"],
            "stitched_oos_sharpe": row["stitched_oos_sharpe"],
            "stitched_oos_max_drawdown": row["stitched_oos_max_drawdown"],
            "survives_cost": float(row["stitched_oos_total_return"]) > 0,
        }
        for row in candidate_rows
    ]


def year_performance(evaluations: dict[tuple[str, int, str], Evaluation]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[float]] = {}
    for (candidate_id, cost, _), evaluation in evaluations.items():
        if cost != 10:
            continue
        for trade in evaluation.trades:
            year = datetime.fromisoformat(str(trade["entry_timestamp"])).year
            grouped.setdefault((candidate_id, year), []).append(float(trade["net_return"]))
    return [
        {
            "candidate_id": candidate_id,
            "year": year,
            "trade_count": len(values),
            "net_return": prod(1 + value for value in values) - 1,
            "average_trade": mean(values),
        }
        for (candidate_id, year), values in sorted(grouped.items())
    ]


def bootstrap_oos(evaluations: dict[tuple[str, int, str], Evaluation]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(CANDIDATES):
        values = np.asarray(
            [
                float(trade["net_return"])
                for (candidate_id, cost, _), evaluation in evaluations.items()
                if candidate_id == candidate.candidate_id and cost == 10
                for trade in evaluation.trades
            ],
            dtype=float,
        )
        if len(values) == 0:
            continue
        rng = np.random.default_rng(BOOTSTRAP_SEED + candidate_index)
        totals: list[float] = []
        averages: list[float] = []
        factors: list[float] = []
        drawdowns: list[float] = []
        for _ in range(BOOTSTRAP_SAMPLES):
            sample = rng.choice(values, size=len(values), replace=True)
            curve = np.cumprod(1 + sample)
            totals.append(float(curve[-1] - 1))
            averages.append(float(np.mean(sample)))
            losses = sample[sample < 0]
            factors.append(
                float(sample[sample > 0].sum() / abs(losses.sum())) if len(losses) else float("inf")
            )
            peaks = np.maximum.accumulate(np.concatenate(([1.0], curve)))
            full_curve = np.concatenate(([1.0], curve))
            drawdowns.append(float(np.min(full_curve / peaks - 1)))
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "trade_count": len(values),
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "net_return_ci_low": float(np.quantile(totals, 0.025)),
                "net_return_ci_high": float(np.quantile(totals, 0.975)),
                "average_trade_ci_low": float(np.quantile(averages, 0.025)),
                "average_trade_ci_high": float(np.quantile(averages, 0.975)),
                "profit_factor_p05": float(np.quantile(factors, 0.05)),
                "profit_factor_p95": float(np.quantile(factors, 0.95)),
                "probability_final_return_below_zero": float(np.mean(np.asarray(totals) < 0)),
                "total_return_p05": float(np.quantile(totals, 0.05)),
                "max_drawdown_p05_worst": float(np.quantile(drawdowns, 0.05)),
            }
        )
    return rows


def trade_removal_sensitivity(
    evaluations: dict[tuple[str, int, str], Evaluation],
) -> list[dict[str, Any]]:
    """Measure whether stitched OOS survives removing its largest winning trades."""
    trades = [
        trade
        for (candidate_id, cost, _), evaluation in evaluations.items()
        if candidate_id == "h24_unfiltered" and cost == 10
        for trade in evaluation.trades
    ]
    return concentration_diagnostics(trades, scope="stitched_oos")


def analyze_fixed_exit_trades(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize the frozen 24H/10bps full-sample trade artifact without rerunning it."""
    trades = [
        dict(row)
        for row in rows
        if int(row["exit_horizon"]) == 24 and int(row["round_trip_cost_bps"]) == 10
    ]
    if not trades:
        raise ValueError("fixed-exit artifact contains no 24H/10bps trades")
    period_rows: list[dict[str, Any]] = []
    for period_type in ("year", "quarter"):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for trade in trades:
            timestamp = datetime.fromisoformat(str(trade["entry_timestamp"]))
            period = (
                str(timestamp.year)
                if period_type == "year"
                else f"{timestamp.year}-Q{(timestamp.month - 1) // 3 + 1}"
            )
            grouped.setdefault(period, []).append(trade)
        for period, items in sorted(grouped.items()):
            returns = [float(item["net_return"]) for item in items]
            period_rows.append(
                {
                    "period_type": period_type,
                    "period": period,
                    "trade_count": len(items),
                    "compounded_return": prod(1 + value for value in returns) - 1,
                    "net_pnl": sum(float(item["net_pnl"]) for item in items),
                    "positive_net_pnl": sum(max(float(item["net_pnl"]), 0.0) for item in items),
                    "average_trade": mean(returns),
                }
            )
    top_periods = sorted(
        (row for row in period_rows if row["period_type"] == "quarter"),
        key=lambda row: float(row["net_pnl"]),
        reverse=True,
    )[:5]
    return {
        "trade_count": len(trades),
        "concentration_rows": concentration_diagnostics(trades, scope="full_sample_fixed_exit"),
        "profit_source_rows": period_rows,
        "top_profit_periods": top_periods,
    }


def concentration_diagnostics(
    trades: Iterable[Mapping[str, Any]], *, scope: str
) -> list[dict[str, Any]]:
    items = [dict(trade) for trade in trades]
    ranked = sorted(
        items,
        key=lambda row: float(row.get("net_pnl", row["net_return"])),
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    for remove_count in (0, 5, 10):
        removed_ids = {str(row["trade_id"]) for row in ranked[:remove_count]}
        retained = [row for row in items if str(row["trade_id"]) not in removed_ids]
        total_return = prod(1 + float(row["net_return"]) for row in retained) - 1
        rows.append(
            {
                "scope": scope,
                "candidate_id": "h24_unfiltered",
                "round_trip_cost_bps": 10,
                "removed_top_winners": remove_count,
                "remaining_trade_count": len(retained),
                "total_return_after_removal": total_return,
                "still_profitable": total_return > 0,
                "removed_trade_ids": "|".join(
                    str(row["trade_id"]) for row in ranked[:remove_count]
                ),
            }
        )
    return rows


def threshold_sensitivity(
    candles: list[Candle],
    episodes: tuple[Episode, ...],
    windows: tuple[WalkForwardWindow, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "check": "VWAP parameter sensitivity",
            "status": "not_run",
            "reason": "fixed-rule V1 forbids OOS parameter selection and production parameter changes",
        },
    ]
    base = next(item for item in CANDIDATES if item.candidate_id == "h24_normal_vol")
    for variant, low_factor, high_factor in (
        ("normal_band_narrower_5pct", 1.05, 0.95),
        ("normal_band_wider_5pct", 0.95, 1.05),
    ):
        returns: list[float] = []
        counts: list[int] = []
        for window in windows:
            low, high = fit_volatility_thresholds(
                candles, episodes, window.train_start_index, window.train_end_index
            )
            evaluation = evaluate_period(
                candles,
                episodes,
                base,
                window.test_start_index,
                window.test_end_index,
                CostModel.equal_split(10),
                (low * low_factor, high * high_factor),
                period_id=window.window_id,
                phase="threshold_sensitivity_diagnostic",
            )
            returns.append(float(evaluation.metrics["total_return"]))
            counts.append(int(evaluation.metrics["trade_count"]))
        rows.append(
            {
                "check": "volatility threshold sensitivity",
                "status": "completed",
                "variant": variant,
                "test_windows_total": len(returns),
                "positive_test_windows": sum(value > 0 for value in returns),
                "stitched_oos_total_return": prod(1 + value for value in returns) - 1,
                "trade_count": sum(counts),
                "selection_eligible": False,
            }
        )
    return rows


def _benchmark_rows(
    candidate: CandidateSpec,
    cost_bps: int,
    window: WalkForwardWindow,
    evaluation: Evaluation,
    unfiltered: Evaluation | None,
) -> list[dict[str, Any]]:
    rows = [
        {
            "window_id": window.window_id,
            "candidate_id": candidate.candidate_id,
            "round_trip_cost_bps": cost_bps,
            "benchmark": "BTC Buy & Hold",
            "return": evaluation.metrics["benchmark_return"],
        },
        {
            "window_id": window.window_id,
            "candidate_id": candidate.candidate_id,
            "round_trip_cost_bps": cost_bps,
            "benchmark": "Random Entry percentile",
            "return": evaluation.metrics["random_entry_percentile"],
        },
    ]
    if candidate.candidate_id != "h24_unfiltered" and candidate.horizon_hours == 24:
        rows.append(
            {
                "window_id": window.window_id,
                "candidate_id": candidate.candidate_id,
                "round_trip_cost_bps": cost_bps,
                "benchmark": "Unfiltered VWAP Episode Strategy",
                "return": unfiltered.metrics["total_return"] if unfiltered else None,
            }
        )
    return rows


def _random_percentile(
    candles: list[Candle],
    trade_count: int,
    horizon: int,
    cost: CostModel,
    actual_return: float,
    identity: str,
) -> float | None:
    if trade_count == 0:
        return None
    seed_offset = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16)
    values: list[float] = []
    for sample_id in range(RANDOM_SAMPLE_COUNT):
        rng = np.random.default_rng(RANDOM_SEED + seed_offset + sample_id)
        candidates = _random_candidates(len(candles), trade_count, horizon, rng)
        total_return, _, _ = _random_portfolio_metrics(candles, candidates, cost)
        values.append(total_return)
    return sum(value <= actual_return for value in values) / len(values)


def _volatility_label(value: float | None, thresholds: tuple[float, float]) -> str:
    if value is None:
        return "insufficient_history"
    low, high = thresholds
    return "low" if value < low else "high" if value > high else "normal"


def _add_rolling_metrics(rows: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row["candidate_id"]), int(row["round_trip_cost_bps"])), []).append(
            row
        )
    lookback = 24 * 30
    for items in grouped.values():
        equities = [float(row["equity"]) for row in items]
        returns = [0.0] + [later / earlier - 1 for earlier, later in pairwise(equities)]
        for index, row in enumerate(items):
            start = max(0, index - lookback + 1)
            window_returns = returns[start : index + 1]
            row["rolling_return_30d"] = equities[index] / equities[start] - 1
            row["rolling_sharpe_30d"] = _sharpe(window_returns)


def _sharpe(returns: list[float]) -> float | None:
    deviation = pstdev(returns) if returns else 0.0
    return mean(returns) / deviation * sqrt(HOURS_PER_YEAR) if deviation > 0 else None


def _drawdown_from_returns(returns: list[float]) -> float:
    curve = [INITIAL_EQUITY]
    for value in returns:
        curve.append(curve[-1] * (1 + value))
    return drawdown_statistics(curve)[0]


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
