"""Deterministic, research-only fixed-exit portfolio simulation for VWAP episodes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from math import prod, sqrt
from statistics import mean, median, pstdev
from typing import Any

import numpy as np

from app.domain.market import Candle
from backtest.vwap_episode_research import EPISODE_INTERVAL, Episode

FIXED_HORIZONS = (6, 12, 24, 48)
COST_SCENARIOS_BPS = (0, 5, 10, 15, 20)
BASELINE_COST_BPS = 10
INITIAL_EQUITY = 100_000.0
RANDOM_SAMPLE_COUNT = 200
RANDOM_SEED = 20260812
MIN_SAMPLE_WARNING = 30
HOURS_PER_YEAR = 365.25 * 24


@dataclass(frozen=True, slots=True)
class CostModel:
    round_trip_cost_bps: int
    entry_fee_bps: float
    exit_fee_bps: float
    entry_slippage_bps: float
    exit_slippage_bps: float

    @classmethod
    def equal_split(cls, total_bps: int) -> CostModel:
        component = total_bps / 4
        return cls(total_bps, component, component, component, component)


@dataclass(frozen=True, slots=True)
class TradeCandidate:
    episode_id: str
    signal_timestamp: str
    entry_index: int
    exit_index: int
    market_regime: str
    volatility_regime: str
    holdout: bool


@dataclass(frozen=True, slots=True)
class FixedExitStudy:
    trade_rows: tuple[dict[str, Any], ...]
    equity_rows: tuple[dict[str, Any], ...]
    metric_rows: tuple[dict[str, Any], ...]
    cost_rows: tuple[dict[str, Any], ...]
    regime_rows: tuple[dict[str, Any], ...]
    holdout_rows: tuple[dict[str, Any], ...]
    random_rows: tuple[dict[str, Any], ...]
    monthly_rows: tuple[dict[str, Any], ...]
    yearly_rows: tuple[dict[str, Any], ...]
    benchmark_rows: tuple[dict[str, Any], ...]
    blocked_by_horizon: dict[int, int]


def run_fixed_exit_study(candles: list[Candle], episodes: tuple[Episode, ...]) -> FixedExitStudy:
    """Run the frozen horizon/cost matrix without overlapping positions."""
    if not candles:
        raise ValueError("fixed-exit research requires candles")
    all_trades: list[dict[str, Any]] = []
    all_equity: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    regimes: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    monthly: list[dict[str, Any]] = []
    yearly: list[dict[str, Any]] = []
    random_rows: list[dict[str, Any]] = []
    blocked: dict[int, int] = {}

    for horizon in FIXED_HORIZONS:
        candidates, blocked_count = select_episode_candidates(candles, episodes, horizon)
        blocked[horizon] = blocked_count
        for cost_bps in COST_SCENARIOS_BPS:
            cost = CostModel.equal_split(cost_bps)
            trade_rows, equity_rows = simulate_portfolio(candles, candidates, horizon, cost)
            row = performance_metrics(
                equity_rows, trade_rows, candles, horizon=horizon, cost_bps=cost_bps
            )
            row["signals_blocked_by_open_position"] = blocked_count
            all_trades.extend(trade_rows)
            all_equity.extend(equity_rows)
            metrics.append(row)
            regimes.extend(regime_performance(trade_rows, horizon, cost_bps))
            holdout.append(holdout_performance(trade_rows, horizon, cost_bps))
            monthly.extend(period_returns(equity_rows, horizon, cost_bps, "month"))
            yearly.extend(period_returns(equity_rows, horizon, cost_bps, "year"))
            random_rows.extend(
                random_benchmark(
                    candles,
                    trade_count=len(candidates),
                    horizon=horizon,
                    cost=cost,
                    actual_metrics=row,
                )
            )

    cost_rows = cost_sensitivity(metrics, all_trades)
    benchmarks = buy_and_hold_benchmark(candles)
    return FixedExitStudy(
        tuple(all_trades),
        tuple(all_equity),
        tuple(metrics),
        tuple(cost_rows),
        tuple(regimes),
        tuple(holdout),
        tuple(random_rows),
        tuple(monthly),
        tuple(yearly),
        tuple(benchmarks),
        blocked,
    )


def select_episode_candidates(
    candles: list[Candle], episodes: tuple[Episode, ...], horizon: int
) -> tuple[tuple[TradeCandidate, ...], int]:
    """Select entries greedily; an entry at the previous exit timestamp is allowed."""
    selected: list[TradeCandidate] = []
    blocked = 0
    next_flat_index = -1
    for episode in episodes:
        entry_index = episode.start_index + 1
        exit_index = entry_index + horizon
        if episode.start_index < next_flat_index:
            blocked += 1
            continue
        if not _continuous_window(candles, episode.start_index, exit_index):
            continue
        selected.append(
            TradeCandidate(
                episode.episode_id,
                episode.start_signal_timestamp,
                entry_index,
                exit_index,
                episode.market_regime,
                episode.volatility_regime,
                episode.holdout,
            )
        )
        next_flat_index = exit_index
    return tuple(selected), blocked


def simulate_portfolio(
    candles: list[Candle],
    candidates: tuple[TradeCandidate, ...],
    horizon: int,
    cost: CostModel,
    *,
    initial_equity: float = INITIAL_EQUITY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compound a fully invested, long-only portfolio and mark it at hourly opens."""
    trade_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    candidate_by_entry = {item.entry_index: item for item in candidates}
    candidate_by_exit = {item.exit_index: item for item in candidates}
    cash = initial_equity
    quantity = 0.0
    current: dict[str, Any] | None = None
    realized_pnl = 0.0
    peak = initial_equity

    for index, candle in enumerate(candles):
        raw_open = float(candle.open)
        exiting = candidate_by_exit.get(index)
        if exiting is not None and current is not None:
            row, cash = _close_trade(current, exiting, raw_open, cost, cash)
            realized_pnl += float(row["net_pnl"])
            trade_rows.append(row)
            quantity = 0.0
            current = None

        entering = candidate_by_entry.get(index)
        if entering is not None:
            if current is not None:
                raise RuntimeError("one-position invariant violated")
            entry_net = raw_open * (1 + cost.entry_slippage_bps / 10_000)
            entry_fee_rate = cost.entry_fee_bps / 10_000
            equity_before = cash
            quantity = cash / (entry_net * (1 + entry_fee_rate))
            entry_fee = quantity * entry_net * entry_fee_rate
            cash = 0.0
            current = {
                "candidate": entering,
                "equity_before": equity_before,
                "entry_raw": raw_open,
                "entry_net": entry_net,
                "entry_fee": entry_fee,
                "quantity": quantity,
            }

        position_value = quantity * raw_open
        equity = cash + position_value
        unrealized = equity - initial_equity - realized_pnl
        peak = max(peak, equity)
        equity_rows.append(
            {
                "horizon_hours": horizon,
                "round_trip_cost_bps": cost.round_trip_cost_bps,
                "timestamp": candle.timestamp.astimezone(UTC).isoformat(),
                "cash": cash,
                "position_value": position_value,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized,
                "equity": equity,
                "drawdown": equity / peak - 1,
            }
        )
    return trade_rows, equity_rows


def _close_trade(
    state: dict[str, Any],
    candidate: TradeCandidate,
    exit_raw: float,
    cost: CostModel,
    cash: float,
) -> tuple[dict[str, Any], float]:
    del cash
    quantity = float(state["quantity"])
    entry_raw = float(state["entry_raw"])
    entry_net = float(state["entry_net"])
    equity_before = float(state["equity_before"])
    entry_fee = float(state["entry_fee"])
    exit_net = exit_raw * (1 - cost.exit_slippage_bps / 10_000)
    exit_fee = quantity * exit_net * cost.exit_fee_bps / 10_000
    final_cash = quantity * exit_net - exit_fee
    gross_return = exit_raw / entry_raw - 1
    net_return = final_cash / equity_before - 1
    gross_pnl = quantity * (exit_raw - entry_raw)
    net_pnl = final_cash - equity_before
    slippage_cost = quantity * ((entry_net - entry_raw) + (exit_raw - exit_net))
    fee_cost = entry_fee + exit_fee
    candles = state.get("candles")
    del candles
    source = state["candidate"]
    if not isinstance(source, TradeCandidate) or source != candidate:
        raise RuntimeError("trade candidate mismatch")
    return {
        "trade_id": f"{candidate.episode_id}-{candidate.exit_index}-{cost.round_trip_cost_bps}",
        "episode_id": candidate.episode_id,
        "signal_timestamp": candidate.signal_timestamp,
        "entry_index": candidate.entry_index,
        "exit_index": candidate.exit_index,
        "entry_price_raw": entry_raw,
        "entry_price_net": entry_net,
        "exit_price_raw": exit_raw,
        "exit_price_net": exit_net,
        "holding_hours": candidate.exit_index - candidate.entry_index,
        "gross_return": gross_return,
        "fee_cost": fee_cost,
        "slippage_cost": slippage_cost,
        "total_cost": fee_cost + slippage_cost,
        "net_return": net_return,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "quantity": quantity,
        "equity_before": equity_before,
        "equity_after": final_cash,
        "market_regime": candidate.market_regime,
        "volatility_regime": candidate.volatility_regime,
        "holdout": candidate.holdout,
        "exit_horizon": candidate.exit_index - candidate.entry_index,
        "round_trip_cost_bps": cost.round_trip_cost_bps,
        "exit_reason": "fixed_horizon",
    }, final_cash


def add_trade_path_metrics(rows: list[dict[str, Any]], candles: list[Candle]) -> None:
    """Add causal MFE/MAE for bars held from entry open until exit open."""
    for row in rows:
        entry = int(row["entry_index"])
        exit_ = int(row["exit_index"])
        entry_price = float(row["entry_price_raw"])
        held = candles[entry:exit_]
        mfe = max(float(item.high) for item in held) / entry_price - 1
        mae = min(float(item.low) for item in held) / entry_price - 1
        row["MFE"] = mfe
        row["MAE"] = mae
        row["captured_fraction_of_mfe"] = float(row["gross_return"]) / mfe if mfe > 0 else None
        row["entry_timestamp"] = candles[entry].timestamp.astimezone(UTC).isoformat()
        row["exit_timestamp"] = candles[exit_].timestamp.astimezone(UTC).isoformat()


def performance_metrics(
    equity_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    candles: list[Candle],
    *,
    horizon: int,
    cost_bps: int,
) -> dict[str, Any]:
    add_trade_path_metrics(trades, candles)
    equities = [float(row["equity"]) for row in equity_rows]
    returns = [later / earlier - 1 for earlier, later in pairwise(equities)]
    elapsed_years = max((len(equities) - 1) / HOURS_PER_YEAR, 1 / HOURS_PER_YEAR)
    total_return = equities[-1] / equities[0] - 1
    cagr = (equities[-1] / equities[0]) ** (1 / elapsed_years) - 1
    max_dd, max_dd_duration, time_under_water = drawdown_statistics(equities)
    net_returns = [float(row["net_return"]) for row in trades]
    wins = [value for value in net_returns if value > 0]
    losses = [value for value in net_returns if value < 0]
    positive_pnl = sorted(
        (float(row["net_pnl"]) for row in trades if float(row["net_pnl"]) > 0), reverse=True
    )
    net_profit = equities[-1] - INITIAL_EQUITY
    return {
        "horizon_hours": horizon,
        "round_trip_cost_bps": cost_bps,
        "trade_count": len(trades),
        "sample_warning": len(trades) < MIN_SAMPLE_WARNING,
        "total_return": total_return,
        "CAGR": cagr,
        "max_drawdown": max_dd,
        "max_drawdown_duration_hours": max_dd_duration,
        "time_under_water_hours": time_under_water,
        "Sharpe": _ratio(returns, downside=False),
        "Sortino": _ratio(returns, downside=True),
        "Calmar": cagr / abs(max_dd) if max_dd < 0 else None,
        "win_rate": len(wins) / len(net_returns) if net_returns else None,
        "average_win": mean(wins) if wins else None,
        "average_loss": mean(losses) if losses else None,
        "payoff_ratio": mean(wins) / abs(mean(losses)) if wins and losses else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "average_trade": mean(net_returns) if net_returns else None,
        "median_trade": median(net_returns) if net_returns else None,
        "average_holding_period": mean([float(row["holding_hours"]) for row in trades])
        if trades
        else None,
        "market_exposure": sum(float(row["holding_hours"]) for row in trades)
        / max(len(candles) - 1, 1),
        "gross_profit": sum(max(float(row["gross_pnl"]), 0) for row in trades),
        "gross_loss": sum(min(float(row["gross_pnl"]), 0) for row in trades),
        "fees_total": sum(float(row["fee_cost"]) for row in trades),
        "slippage_total": sum(float(row["slippage_cost"]) for row in trades),
        "cost_total": sum(float(row["total_cost"]) for row in trades),
        "net_profit": net_profit,
        "top_1_trade_contribution": sum(positive_pnl[:1]) / net_profit if net_profit > 0 else None,
        "top_5_trade_contribution": sum(positive_pnl[:5]) / net_profit if net_profit > 0 else None,
        "top_10_trade_contribution": sum(positive_pnl[:10]) / net_profit
        if net_profit > 0
        else None,
    }


def drawdown_statistics(equities: list[float]) -> tuple[float, int, int]:
    peak = equities[0]
    max_drawdown = 0.0
    current_duration = 0
    max_duration = 0
    underwater = 0
    for equity in equities:
        if equity >= peak:
            peak = equity
            current_duration = 0
        else:
            current_duration += 1
            underwater += 1
            max_duration = max(max_duration, current_duration)
            max_drawdown = min(max_drawdown, equity / peak - 1)
    return max_drawdown, max_duration, underwater


def _ratio(returns: list[float], *, downside: bool) -> float | None:
    if not returns:
        return None
    denominator_values = [min(value, 0.0) for value in returns] if downside else returns
    deviation = pstdev(denominator_values)
    return mean(returns) / deviation * sqrt(HOURS_PER_YEAR) if deviation > 0 else None


def regime_performance(
    trades: list[dict[str, Any]], horizon: int, cost_bps: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dimension in ("market_regime", "volatility_regime"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in trades:
            groups[str(trade[dimension])].append(trade)
        for group, items in sorted(groups.items()):
            values = [float(item["net_return"]) for item in items]
            curve = [INITIAL_EQUITY]
            for value in values:
                curve.append(curve[-1] * (1 + value))
            losses = [value for value in values if value < 0]
            rows.append(
                {
                    "horizon_hours": horizon,
                    "round_trip_cost_bps": cost_bps,
                    "dimension": dimension,
                    "group": group,
                    "trade_count": len(items),
                    "net_return": prod(1 + value for value in values) - 1,
                    "win_rate": sum(value > 0 for value in values) / len(values)
                    if values
                    else None,
                    "profit_factor": sum(max(value, 0) for value in values) / abs(sum(losses))
                    if losses
                    else None,
                    "avg_trade": mean(values) if values else None,
                    "max_drawdown": drawdown_statistics(curve)[0],
                }
            )
    return rows


def holdout_performance(
    trades: list[dict[str, Any]], horizon: int, cost_bps: int
) -> dict[str, Any]:
    values = [float(row["net_return"]) for row in trades if bool(row["holdout"])]
    curve = [INITIAL_EQUITY]
    for value in values:
        curve.append(curve[-1] * (1 + value))
    losses = [value for value in values if value < 0]
    return {
        "horizon_hours": horizon,
        "round_trip_cost_bps": cost_bps,
        "holdout_trade_count": len(values),
        "holdout_net_return": curve[-1] / INITIAL_EQUITY - 1,
        "holdout_win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "holdout_profit_factor": sum(max(value, 0) for value in values) / abs(sum(losses))
        if losses
        else None,
        "holdout_max_drawdown": drawdown_statistics(curve)[0],
    }


def period_returns(
    equity_rows: list[dict[str, Any]], horizon: int, cost_bps: int, period: str
) -> list[dict[str, Any]]:
    last_by_period: dict[str, float] = {}
    for row in equity_rows:
        stamp = datetime.fromisoformat(str(row["timestamp"]))
        key = f"{stamp.year:04d}-{stamp.month:02d}" if period == "month" else str(stamp.year)
        last_by_period[key] = float(row["equity"])
    previous = INITIAL_EQUITY
    result: list[dict[str, Any]] = []
    for key, equity in sorted(last_by_period.items()):
        result.append(
            {
                "horizon_hours": horizon,
                "round_trip_cost_bps": cost_bps,
                "period": key,
                "return": equity / previous - 1,
            }
        )
        previous = equity
    return result


def random_benchmark(
    candles: list[Candle],
    *,
    trade_count: int,
    horizon: int,
    cost: CostModel,
    actual_metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if trade_count == 0:
        return results
    for sample_id in range(RANDOM_SAMPLE_COUNT):
        rng = np.random.default_rng(RANDOM_SEED + horizon * 10_000 + sample_id)
        candidates = _random_candidates(len(candles), trade_count, horizon, rng)
        total_return, sharpe, max_drawdown = _random_portfolio_metrics(candles, candidates, cost)
        results.append(
            {
                "horizon_hours": horizon,
                "round_trip_cost_bps": cost.round_trip_cost_bps,
                "sample_id": sample_id,
                "seed": RANDOM_SEED + horizon * 10_000 + sample_id,
                "trade_count": trade_count,
                "total_return": total_return,
                "Sharpe": sharpe,
                "max_drawdown": max_drawdown,
            }
        )
    actual_return = float(actual_metrics["total_return"])
    actual_sharpe = _optional_float(actual_metrics["Sharpe"])
    actual_dd = float(actual_metrics["max_drawdown"])
    for row in results:
        row["actual_return_percentile"] = sum(
            float(item["total_return"]) <= actual_return for item in results
        ) / len(results)
        row["actual_sharpe_percentile"] = (
            sum(
                float(item["Sharpe"] if item["Sharpe"] is not None else float("-inf"))
                <= actual_sharpe
                for item in results
            )
            / len(results)
            if actual_sharpe is not None
            else None
        )
        row["actual_max_dd_percentile"] = sum(
            float(item["max_drawdown"]) <= actual_dd for item in results
        ) / len(results)
    return results


def _random_portfolio_metrics(
    candles: list[Candle],
    candidates: tuple[TradeCandidate, ...],
    cost: CostModel,
) -> tuple[float, float | None, float]:
    """Compute hourly marked equity without constructing unused ledger rows."""
    prices = np.asarray([float(candle.open) for candle in candles], dtype=float)
    equity = np.full(len(candles), INITIAL_EQUITY, dtype=float)
    current_equity = INITIAL_EQUITY
    cursor = 0
    entry_slippage = cost.entry_slippage_bps / 10_000
    entry_fee = cost.entry_fee_bps / 10_000
    exit_slippage = cost.exit_slippage_bps / 10_000
    exit_fee = cost.exit_fee_bps / 10_000
    for candidate in candidates:
        equity[cursor : candidate.entry_index] = current_equity
        entry_price = prices[candidate.entry_index]
        quantity = current_equity / (entry_price * (1 + entry_slippage) * (1 + entry_fee))
        equity[candidate.entry_index : candidate.exit_index] = (
            quantity * prices[candidate.entry_index : candidate.exit_index]
        )
        current_equity = (
            quantity * prices[candidate.exit_index] * (1 - exit_slippage) * (1 - exit_fee)
        )
        equity[candidate.exit_index] = current_equity
        cursor = candidate.exit_index + 1
    equity[cursor:] = current_equity
    hourly_returns = np.diff(equity) / equity[:-1]
    deviation = float(np.std(hourly_returns))
    sharpe = (
        float(np.mean(hourly_returns)) / deviation * sqrt(HOURS_PER_YEAR) if deviation > 0 else None
    )
    peaks = np.maximum.accumulate(equity)
    max_drawdown = float(np.min(equity / peaks - 1))
    return float(equity[-1] / INITIAL_EQUITY - 1), sharpe, max_drawdown


def _random_candidates(
    candle_count: int, trade_count: int, horizon: int, rng: np.random.Generator
) -> tuple[TradeCandidate, ...]:
    maximum = candle_count - 1 - horizon
    adjusted_maximum = maximum - (trade_count - 1) * (horizon - 1)
    if adjusted_maximum < trade_count:
        raise ValueError("random benchmark cannot match requested trade count")
    sampled = sorted(
        int(value)
        for value in rng.choice(np.arange(1, adjusted_maximum + 1), size=trade_count, replace=False)
    )
    entries = [value + index * (horizon - 1) for index, value in enumerate(sampled)]
    return tuple(
        TradeCandidate(
            f"random-{index}",
            candles_timestamp_placeholder(entry),
            entry,
            entry + horizon,
            "random",
            "random",
            False,
        )
        for index, entry in enumerate(entries)
    )


def candles_timestamp_placeholder(index: int) -> str:
    return f"random-index-{index}"


def cost_sensitivity(
    metrics: list[dict[str, Any]], trades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for horizon in FIXED_HORIZONS:
        items = [row for row in metrics if int(row["horizon_hours"]) == horizon]
        zero_cost_trades = [
            row
            for row in trades
            if int(row["exit_horizon"]) == horizon and int(row["round_trip_cost_bps"]) == 0
        ]
        break_even_bps = _break_even_cost_bps(zero_cost_trades)
        for row in items:
            rows.append(
                {
                    "horizon_hours": horizon,
                    "round_trip_cost_bps": row["round_trip_cost_bps"],
                    "total_return": row["total_return"],
                    "average_trade": row["average_trade"],
                    "break_even_round_trip_cost_bps": break_even_bps,
                    "high_cost_fragility": break_even_bps <= 10,
                }
            )
    return rows


def _break_even_cost_bps(trades: list[dict[str, Any]]) -> float:
    """Solve the cost at which compounded terminal equity reaches break-even."""
    gross_factors = [float(row["exit_price_raw"]) / float(row["entry_price_raw"]) for row in trades]
    if not gross_factors or prod(gross_factors) <= 1:
        return 0.0

    def terminal_factor(total_bps: float) -> float:
        component = total_bps / 4 / 10_000
        friction = (1 - component) ** 2 / (1 + component) ** 2
        return prod(factor * friction for factor in gross_factors)

    lower, upper = 0.0, 10_000.0
    for _ in range(80):
        middle = (lower + upper) / 2
        if terminal_factor(middle) > 1:
            lower = middle
        else:
            upper = middle
    return (lower + upper) / 2


def buy_and_hold_benchmark(candles: list[Candle]) -> list[dict[str, Any]]:
    prices = [float(item.open) for item in candles]
    returns = [later / earlier - 1 for earlier, later in pairwise(prices)]
    years = (len(prices) - 1) / HOURS_PER_YEAR
    total = prices[-1] / prices[0] - 1
    return [
        {
            "benchmark": "BTC Buy & Hold",
            "total_return": total,
            "CAGR": (1 + total) ** (1 / years) - 1,
            "max_drawdown": drawdown_statistics(prices)[0],
            "Sharpe": _ratio(returns, downside=False),
        }
    ]


def _continuous_window(candles: list[Candle], start: int, end: int) -> bool:
    if start < 0 or end >= len(candles):
        return False
    return all(
        later.timestamp - earlier.timestamp == EPISODE_INTERVAL
        for earlier, later in zip(candles[start:end], candles[start + 1 : end + 1], strict=True)
    )


def _optional_float(value: object, default: float | None = None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default
