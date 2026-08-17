"""Static comparison charts for Strategy Research V2 artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from backtest.strategy_v2_research import StrategyV2Study


def render_strategy_v2_charts(output: Path, study: StrategyV2Study) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    scoreboard = list(study.scoreboard)
    labels = [str(row["candidate_id"]) for row in scoreboard]
    _bars(
        output / "01_forward_edge_comparison.png",
        labels,
        [_number(row["forward_edge"], percent=True) for row in scoreboard],
        "24H excess vs unconditional",
    )
    _bars(
        output / "02_random_percentiles.png",
        labels,
        [_number(row["random_percentile"], percent=True) for row in scoreboard],
        "24H random benchmark percentile",
    )
    _grouped_cost(output / "03_cost_sensitivity.png", scoreboard)
    _bars(
        output / "04_max_drawdown.png",
        labels,
        [_number(row["max_drawdown"], percent=True) for row in scoreboard],
        "Validation max drawdown",
    )
    _bars(
        output / "05_sharpe.png",
        labels,
        [_number(row["Sharpe"]) for row in scoreboard],
        "Validation Sharpe",
    )
    _bars(
        output / "06_oos_performance.png",
        labels,
        [_number(row["walk_forward_return"], percent=True) for row in scoreboard],
        "Stitched walk-forward OOS return",
    )
    _year_chart(output / "07_year_by_year.png", study)
    _bars(
        output / "08_profit_concentration.png",
        labels,
        [_number(row["top5_concentration"], percent=True) for row in scoreboard],
        "Return lost after removing top 5 winners",
    )
    _status_chart(output / "09_candidate_scoreboard.png", scoreboard)
    for index, result in enumerate(study.results):
        directory = output / f"candidate_{chr(ord('A') + index)}"
        _candidate_forward_chart(directory / "forward_edge.png", result.forward_rows)


def _grouped_cost(path: Path, rows: list[dict[str, Any]]) -> None:
    labels = [str(row["candidate_id"]) for row in rows]
    positions = list(range(len(rows)))
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar(
        [value - 0.18 for value in positions],
        [_number(row["10bps_return"], percent=True) for row in rows],
        0.36,
        label="10 bps",
    )
    axis.bar(
        [value + 0.18 for value in positions],
        [_number(row["20bps_return"], percent=True) for row in rows],
        0.36,
        label="20 bps",
    )
    axis.set_xticks(positions, labels, rotation=20)
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_title("Validation cost sensitivity")
    axis.legend()
    _save(figure, path)


def _year_chart(path: Path, study: StrategyV2Study) -> None:
    figure, axis = plt.subplots(figsize=(11, 5))
    for result in study.results:
        axis.plot(
            [row["year"] for row in result.yearly_rows],
            [float(row["mean_24h_return"]) * 100 for row in result.yearly_rows],
            marker="o",
            label=result.candidate_id,
        )
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_title("Mean 24H forward return by year")
    axis.legend(fontsize=8)
    _save(figure, path)


def _status_chart(path: Path, rows: list[dict[str, Any]]) -> None:
    stage = {"forward_edge": 1, "fixed_exit": 2, "walk_forward": 3}
    _bars(
        path,
        [str(row["candidate_id"]) for row in rows],
        [stage[str(row["stage_reached"])] for row in rows],
        "Candidate elimination stage reached",
    )


def _candidate_forward_chart(path: Path, rows: tuple[dict[str, Any], ...]) -> None:
    selected = [row for row in rows if row["scope"] == "development"]
    _bars(
        path,
        [f"{row['horizon_hours']}H" for row in selected],
        [_number(row["excess_vs_unconditional"], percent=True) for row in selected],
        "Forward excess by horizon",
    )


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
