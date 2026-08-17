"""Required Phase A descriptive charts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure


def render_market_information_charts(
    output: Path, rows: list[dict[str, Any]], study: dict[str, Any]
) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    _line(output / "01_oi_history.png", rows, "open_interest_btc", "OI history (BTC)")
    _line(output / "02_funding_history.png", rows, "funding_rate", "Funding history")
    _line(output / "03_basis_history.png", rows, "basis_pct", "Spot-perpetual basis")
    _two_lines(
        output / "04_price_vs_oi.png", rows, "spot_close", "open_interest_btc", "Price vs OI"
    )
    _state_chart(
        output / "05_funding_vs_future_returns.png",
        study["analyses"]["funding"],
        "Funding vs 24H returns",
    )
    _state_chart(
        output / "06_basis_vs_future_returns.png",
        study["analyses"]["basis"],
        "Basis vs 24H returns",
    )
    _state_chart(
        output / "07_oi_change_vs_future_returns.png",
        study["analyses"]["oi"],
        "OI direction vs 24H returns",
    )
    _state_chart(
        output / "08_price_oi_quadrants.png",
        study["analyses"]["price_oi_quadrants"],
        "Price/OI quadrant comparison",
    )
    _ic_chart(output / "09_information_coefficient.png", study["information_coefficients"])
    _coverage_chart(output / "10_data_coverage_timeline.png", rows)
    _scoreboard_chart(output / "11_information_scoreboard.png", study["scoreboard"])


def _line(path: Path, rows: list[dict[str, Any]], field: str, title: str) -> None:
    points = [
        (index, float(row[field])) for index, row in enumerate(rows) if row.get(field) is not None
    ]
    figure, axis = plt.subplots(figsize=(11, 4))
    axis.plot([item[0] for item in points], [item[1] for item in points])
    axis.set_title(title)
    _save(figure, path)


def _two_lines(path: Path, rows: list[dict[str, Any]], left: str, right: str, title: str) -> None:
    selected = [
        (index, float(row[left]), float(row[right]))
        for index, row in enumerate(rows)
        if row.get(left) is not None and row.get(right) is not None
    ]
    figure, axis = plt.subplots(figsize=(11, 4))
    twin = axis.twinx()
    axis.plot([item[0] for item in selected], [item[1] for item in selected], label=left)
    twin.plot(
        [item[0] for item in selected],
        [item[2] for item in selected],
        color="tab:orange",
        label=right,
    )
    axis.set_title(title)
    _save(figure, path)


def _state_chart(path: Path, rows: list[dict[str, Any]], title: str) -> None:
    selected = [row for row in rows if int(row["horizon_hours"]) == 24]
    _bars(
        path,
        [str(row["state"]) for row in selected],
        [float(row["excess_vs_unconditional"] or 0) * 100 for row in selected],
        title,
    )


def _ic_chart(path: Path, rows: list[dict[str, Any]]) -> None:
    selected = [row for row in rows if row["period"] == "all" and int(row["horizon_hours"]) == 24]
    _bars(
        path,
        [str(row["feature"]) for row in selected],
        [float(row["spearman_ic"] or 0) for row in selected],
        "24H information coefficient",
    )


def _coverage_chart(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ("basis_pct", "funding_rate", "open_interest_btc")
    _bars(
        path,
        list(fields),
        [
            sum(row.get(field) is not None for row in rows) / max(len(rows), 1) * 100
            for field in fields
        ],
        "Data coverage (%)",
    )


def _scoreboard_chart(path: Path, rows: list[dict[str, Any]]) -> None:
    labels = [f"{row['feature']}:{row['state_or_transformation']}" for row in rows]
    values = [float(row["24h_excess"] or 0) * 100 for row in rows]
    _bars(path, labels, values, "Information scoreboard 24H excess")


def _bars(path: Path, labels: list[str], values: list[float], title: str) -> None:
    figure, axis = plt.subplots(figsize=(11, 5))
    axis.bar(labels, values)
    axis.axhline(0, color="black", linewidth=0.8)
    axis.tick_params(axis="x", rotation=35)
    axis.set_title(title)
    _save(figure, path)


def _save(figure: Figure, path: Path) -> None:
    figure.tight_layout()
    figure.savefig(path, dpi=140)
    plt.close(figure)
