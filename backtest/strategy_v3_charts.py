"""Comparison charts for Strategy Research V3."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from backtest.strategy_v3_research import StrategyV3Study


def render_strategy_v3_charts(output: Path, study: StrategyV3Study) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    rows = list(study.scoreboard)
    labels = [str(row["candidate_id"]) for row in rows]
    _bars(
        output / "01_forward_edge.png",
        labels,
        [_number(row["24h_excess"], percent=True) for row in rows],
        "24H forward excess",
    )
    _bars(
        output / "02_htf_incremental_value.png",
        labels,
        [_number(row["htf_incremental_delta"], percent=True) for row in rows],
        "HTF incremental value",
    )
    _bars(
        output / "03_volume_incremental_value.png",
        labels,
        [_number(row["volume_incremental_delta"], percent=True) for row in rows],
        "Volume incremental value",
    )
    _bars(
        output / "04_random_percentile.png",
        labels,
        [_number(row["random_percentile"], percent=True) for row in rows],
        "Random benchmark percentile",
    )
    _bars(
        output / "05_fixed_exit_performance.png",
        labels,
        [_number(row["return_10bps"], percent=True) for row in rows],
        "Validation fixed exit return (10 bps)",
    )
    _cost_chart(output / "06_cost_sensitivity.png", rows)
    _bars(
        output / "07_max_drawdown.png",
        labels,
        [_number(row["max_drawdown"], percent=True) for row in rows],
        "Validation max drawdown",
    )
    _bars(
        output / "08_profit_concentration.png",
        labels,
        [_number(row["remove_top5_result"], percent=True) for row in rows],
        "Return after removing top 5 winners",
    )
    _bars(
        output / "09_walk_forward_oos.png",
        labels,
        [_number(row["walk_forward_return"], percent=True) for row in rows],
        "Stitched walk-forward OOS return",
    )
    stages = {"forward_edge": 1, "fixed_exit": 2, "walk_forward": 3}
    _bars(
        output / "10_candidate_scoreboard.png",
        labels,
        [stages[str(row["stage_reached"])] for row in rows],
        "Candidate stage reached",
    )
    for index, result in enumerate(study.results):
        directory = output / f"candidate_{chr(ord('A') + index)}"
        development = [row for row in result.forward_rows if row["scope"] == "development"]
        _bars(
            directory / "forward_edge.png",
            [f"{row['horizon_hours']}H" for row in development],
            [_number(row["excess_vs_unconditional"], percent=True) for row in development],
            "Forward excess by horizon",
        )


def _cost_chart(path: Path, rows: list[dict[str, Any]]) -> None:
    positions = list(range(len(rows)))
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar(
        [value - 0.18 for value in positions],
        [_number(row["return_10bps"], percent=True) for row in rows],
        0.36,
        label="10 bps",
    )
    axis.bar(
        [value + 0.18 for value in positions],
        [_number(row["return_20bps"], percent=True) for row in rows],
        0.36,
        label="20 bps",
    )
    axis.set_xticks(positions, [str(row["candidate_id"]) for row in rows], rotation=20)
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_title("Validation cost sensitivity")
    axis.legend()
    _save(figure, path)


def _bars(path: Path, labels: list[str], values: list[float], title: str) -> None:
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar(labels, values)
    axis.axhline(0, color="black", linewidth=0.8)
    axis.tick_params(axis="x", rotation=20)
    axis.set_title(title)
    _save(figure, path)


def _number(value: object, *, percent: bool = False) -> float:
    result = float(value) if isinstance(value, (int, float)) else 0.0
    return result * 100 if percent else result


def _save(figure: Figure, path: Path) -> None:
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)
