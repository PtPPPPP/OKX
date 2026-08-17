"""PNG chart rendering for the read-only VWAP signal-edge study."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from app.domain.market import Candle
from backtest.vwap_signal_edge import CORE_HORIZONS, COST_HORIZONS, SignalEdgeStudy


def render_signal_edge_charts(
    output: Path,
    study: SignalEdgeStudy,
    candles: list[Candle],
    parameter_rows: list[dict[str, object]],
) -> None:
    """Render the complete, fixed chart set into ``output``."""
    plt.style.use("seaborn-v0_8-whitegrid")
    timestamps = [candle.timestamp.timestamp() / 86400 for candle in candles]
    closes = [float(candle.close) for candle in candles]
    signal_datetimes = [
        datetime.fromisoformat(item.signal_timestamp) for item in study.observations
    ]
    signal_times = [value.timestamp() / 86400 for value in signal_datetimes]
    price_by_time = {candle.timestamp: float(candle.close) for candle in candles}
    signal_prices = [price_by_time[time] for time in signal_datetimes]

    figure, axis = plt.subplots(figsize=(12, 5))
    axis.plot(timestamps, closes, linewidth=0.7)
    axis.scatter(signal_times, signal_prices, s=8, color="red")
    axis.set_title("VWAP BUY signal locations")
    _save(figure, output / "01_buy_signal_locations.png")

    episode_forward = [
        row
        for row in study.forward_statistics
        if row["scope"] == "episode" and row["horizon_hours"] in CORE_HORIZONS
    ]
    horizons = [_integer(row["horizon_hours"]) for row in episode_forward]
    _bar_chart(
        output / "02_forward_return_by_horizon.png",
        horizons,
        [_number(row["median"]) * 100 for row in episode_forward],
        title="Median forward return by horizon",
        ylabel="percent",
    )

    figure, axis = plt.subplots()
    axis.bar(horizons, [_number(row["positive_rate"]) * 100 for row in episode_forward])
    axis.axhline(50, color="black", linewidth=0.8)
    axis.set_title("Positive rate by horizon")
    axis.set_ylabel("percent")
    _save(figure, output / "03_positive_rate_by_horizon.png")

    for metric, number in (("mfe", 4), ("mae", 5)):
        rows = [row for row in study.mfe_mae_statistics if row["metric"] == metric]
        _bar_chart(
            output / f"{number:02d}_{metric}_by_horizon.png",
            [_integer(row["horizon_hours"]) for row in rows],
            [_number(row["median"]) * 100 for row in rows],
            title=f"Median {metric.upper()} by horizon",
            ylabel="percent",
        )

    regimes = [
        row
        for row in study.regime_statistics
        if row["scope"] == "episode"
        and row["dimension"] == "market_regime"
        and row["horizon_hours"] == 24
    ]
    _bar_chart(
        output / "06_regime_comparison.png",
        [str(row["group"]) for row in regimes],
        [_number(row["median_return"]) * 100 for row in regimes],
        title="24H return by market regime",
    )

    months = list(study.monthly_statistics)
    figure, axis = plt.subplots(figsize=(12, 5))
    axis.bar(
        [str(row["period"]) for row in months],
        [_number(row["median_return_24h"]) * 100 for row in months],
    )
    axis.tick_params(axis="x", rotation=90)
    axis.set_title("Monthly 24H median signal return")
    _save(figure, output / "07_monthly_signal_edge.png")

    observations_24h = [item for item in study.observations if item.returns[24] is not None]
    figure, axis = plt.subplots()
    axis.scatter(
        [item.deviation_bps for item in observations_24h],
        [float(item.returns[24] or 0) * 100 for item in observations_24h],
        s=8,
        alpha=0.35,
    )
    axis.set_xlabel("deviation bps")
    axis.set_ylabel("24H return percent")
    axis.set_title("Signal deviation vs 24H return")
    _save(figure, output / "08_deviation_vs_forward_return.png")

    episode_costs = [
        row
        for row in study.cost_statistics
        if row["scope"] == "episode" and row["horizon_hours"] in COST_HORIZONS
    ]
    figure, axis = plt.subplots()
    for horizon in COST_HORIZONS:
        rows = [row for row in episode_costs if row["horizon_hours"] == horizon]
        axis.plot(
            [_integer(row["assumed_round_trip_cost_bps"]) for row in rows],
            [_number(row["median_net_forward_return"]) * 100 for row in rows],
            marker="o",
            label=f"{horizon}H",
        )
    axis.legend()
    axis.set_title("Signal Edge After Hypothetical Cost")
    axis.set_xlabel("round-trip cost bps")
    axis.set_ylabel("median net forward return percent")
    _save(figure, output / "09_cost_sensitivity.png")

    _parameter_sensitivity_chart(output, parameter_rows)

    figure, axis = plt.subplots(figsize=(12, 2.8))
    axis.scatter(signal_times, [1] * len(signal_times), s=8)
    axis.set_yticks([])
    axis.set_title("Signal clustering timeline")
    _save(figure, output / "11_signal_clustering_timeline.png")


def _parameter_sensitivity_chart(output: Path, parameter_rows: list[dict[str, object]]) -> None:
    windows = (20, 24, 28)
    deviations = (80, 100, 120)
    matrix = np.full((len(windows), len(deviations)), np.nan)
    for row in parameter_rows:
        if row["horizon_hours"] != 24:
            continue
        matrix[windows.index(_integer(row["vwap_window"]))][
            deviations.index(_integer(row["buy_deviation_bps"]))
        ] = _number(row["median_return"]) * 100
    figure, axis = plt.subplots()
    image = axis.imshow(matrix, cmap="RdYlGn")
    axis.set_xticks(range(len(deviations)), [str(value) for value in deviations])
    axis.set_yticks(range(len(windows)), [str(value) for value in windows])
    axis.set_xlabel("buy deviation bps")
    axis.set_ylabel("VWAP window")
    axis.set_title("24H median return parameter sensitivity")
    figure.colorbar(image, ax=axis)
    _save(figure, output / "10_parameter_sensitivity.png")


def _bar_chart(
    path: Path,
    labels: list[int] | list[str],
    values: list[float],
    *,
    title: str,
    ylabel: str | None = None,
) -> None:
    figure, axis = plt.subplots()
    axis.bar(labels, values)
    axis.set_title(title)
    if ylabel is not None:
        axis.set_ylabel(ylabel)
    _save(figure, path)


def _save(figure: Any, path: Path) -> None:
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _number(value: object, *, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _integer(value: object) -> int:
    if not isinstance(value, (int, float)):
        raise TypeError(f"expected numeric integer, got {type(value).__name__}")
    return int(value)
