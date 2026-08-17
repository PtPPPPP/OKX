"""Static charts for VWAP walk-forward research artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from backtest.vwap_walk_forward_research import WalkForwardStudy


def render_walk_forward_charts(output: Path, study: WalkForwardStudy) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    primary = _stitched(study, "h24_unfiltered", 10)
    _line(output / "01_stitched_oos_equity.png", primary, "equity", "Stitched OOS equity")
    _line(output / "02_oos_drawdown.png", primary, "drawdown", "Stitched OOS drawdown")
    _window_chart(
        output / "03_return_by_test_window.png", study, "total_return", "Return by test window"
    )
    _window_chart(
        output / "04_profit_factor_by_test_window.png",
        study,
        "profit_factor",
        "Profit factor by test window",
    )
    _candidate_chart(output / "05_candidate_comparison.png", study)
    _filtered_chart(output / "06_filtered_vs_unfiltered.png", study)
    _holdout_chart(output / "07_final_holdout.png", study)
    _cost_chart(output / "08_cost_stress.png", study)
    _line(output / "09_rolling_sharpe.png", primary, "rolling_sharpe_30d", "Rolling 30-day Sharpe")
    _year_chart(output / "10_year_by_year.png", study)


def _stitched(study: WalkForwardStudy, candidate_id: str, cost_bps: int) -> list[dict[str, Any]]:
    return [
        row
        for row in study.stitched_rows
        if row["candidate_id"] == candidate_id and int(row["round_trip_cost_bps"]) == cost_bps
    ]


def _line(path: Path, rows: list[dict[str, Any]], field: str, title: str) -> None:
    figure, axis = plt.subplots(figsize=(12, 5))
    axis.plot([str(row["timestamp"])[:10] for row in rows], [row[field] for row in rows])
    axis.tick_params(axis="x", labelbottom=False)
    axis.set_title(title)
    _save(figure, path)


def _window_chart(path: Path, study: WalkForwardStudy, field: str, title: str) -> None:
    rows = [
        row
        for row in study.window_rows
        if row["candidate_id"] == "h24_unfiltered" and int(row["round_trip_cost_bps"]) == 10
    ]
    _bars(
        path, [str(row["window_id"]) for row in rows], [_number(row[field]) for row in rows], title
    )


def _candidate_chart(path: Path, study: WalkForwardStudy) -> None:
    rows = [row for row in study.candidate_rows if int(row["round_trip_cost_bps"]) == 10]
    _bars(
        path,
        [str(row["candidate_id"]) for row in rows],
        [float(row["stitched_oos_total_return"]) * 100 for row in rows],
        "Candidate stitched OOS comparison (10 bps)",
        rotation=25,
    )


def _filtered_chart(path: Path, study: WalkForwardStudy) -> None:
    ids = ("h24_unfiltered", "h24_normal_vol", "h24_exclude_high_vol")
    rows = [
        row
        for row in study.candidate_rows
        if row["candidate_id"] in ids and int(row["round_trip_cost_bps"]) == 10
    ]
    _bars(
        path,
        [str(row["candidate_id"]) for row in rows],
        [float(row["stitched_oos_total_return"]) * 100 for row in rows],
        "Filtered vs unfiltered stitched OOS return",
        rotation=20,
    )


def _holdout_chart(path: Path, study: WalkForwardStudy) -> None:
    rows = [row for row in study.holdout_rows if int(row["round_trip_cost_bps"]) == 10]
    _bars(
        path,
        [str(row["candidate_id"]) for row in rows],
        [float(row["total_return"]) * 100 for row in rows],
        "Final untouched holdout return (10 bps)",
        rotation=25,
    )


def _cost_chart(path: Path, study: WalkForwardStudy) -> None:
    rows = [row for row in study.cost_rows if row["candidate_id"] == "h24_unfiltered"]
    _bars(
        path,
        [f"{row['round_trip_cost_bps']} bps" for row in rows],
        [float(row["stitched_oos_total_return"]) * 100 for row in rows],
        "24H unfiltered OOS cost stress",
    )


def _year_chart(path: Path, study: WalkForwardStudy) -> None:
    rows = [row for row in study.year_rows if row["candidate_id"] == "h24_unfiltered"]
    _bars(
        path,
        [str(row["year"]) for row in rows],
        [float(row["net_return"]) * 100 for row in rows],
        "24H unfiltered OOS year-by-year return",
    )


def _bars(
    path: Path,
    labels: list[str],
    values: list[float],
    title: str,
    *,
    rotation: int = 0,
) -> None:
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.bar(labels, values)
    axis.axhline(0, color="black", linewidth=0.8)
    axis.tick_params(axis="x", rotation=rotation)
    axis.set_title(title)
    _save(figure, path)


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _save(figure: Figure, path: Path) -> None:
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)
