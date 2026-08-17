"""Static charts for the research-only fixed-exit study."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from backtest.vwap_fixed_exit_research import BASELINE_COST_BPS, FIXED_HORIZONS, FixedExitStudy


def render_fixed_exit_charts(output: Path, study: FixedExitStudy) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    _curve_chart(output / "01_equity_curve_by_horizon.png", study, "equity")
    _curve_chart(output / "02_drawdown_by_horizon.png", study, "drawdown")
    _benchmark_chart(output / "03_vwap_vs_buy_hold.png", study)
    _cost_chart(output / "04_cost_sensitivity.png", study)
    _horizon_chart(output / "05_horizon_performance.png", study)
    _monthly_chart(output / "06_monthly_returns.png", study)
    _trade_distribution(output / "07_trade_pnl_distribution.png", study)
    _mfe_chart(output / "08_mfe_vs_realized_return.png", study)
    _holdout_chart(output / "09_recent_holdout_comparison.png", study)


def _curve_chart(path: Path, study: FixedExitStudy, field: str) -> None:
    figure, axis = plt.subplots(figsize=(12, 5))
    for horizon in FIXED_HORIZONS:
        rows = _equity(study, horizon)
        axis.plot(
            [row["timestamp"][:10] for row in rows],
            [float(row[field]) for row in rows],
            label=f"{horizon}H",
            linewidth=0.8,
        )
    axis.legend()
    axis.tick_params(axis="x", labelbottom=False)
    axis.set_title(f"{field.title()} by horizon (10 bps)")
    _save(figure, path)


def _benchmark_chart(path: Path, study: FixedExitStudy) -> None:
    rows = _baseline_metrics(study)
    labels = [f"VWAP {row['horizon_hours']}H" for row in rows] + ["Buy & Hold"]
    values = [float(row["total_return"]) * 100 for row in rows] + [
        float(study.benchmark_rows[0]["total_return"]) * 100
    ]
    _bars(path, labels, values, "VWAP fixed exit vs Buy & Hold", "total return percent")


def _cost_chart(path: Path, study: FixedExitStudy) -> None:
    figure, axis = plt.subplots()
    for horizon in FIXED_HORIZONS:
        rows = [row for row in study.cost_rows if int(row["horizon_hours"]) == horizon]
        axis.plot(
            [row["round_trip_cost_bps"] for row in rows],
            [float(row["total_return"]) * 100 for row in rows],
            marker="o",
            label=f"{horizon}H",
        )
    axis.axhline(0, color="black", linewidth=0.8)
    axis.legend()
    axis.set_title("Cost sensitivity")
    axis.set_xlabel("round-trip bps")
    axis.set_ylabel("total return percent")
    _save(figure, path)


def _horizon_chart(path: Path, study: FixedExitStudy) -> None:
    rows = _baseline_metrics(study)
    _bars(
        path,
        [f"{row['horizon_hours']}H" for row in rows],
        [float(row["Sharpe"] or 0) for row in rows],
        "Horizon risk-adjusted performance (10 bps)",
        "Sharpe",
    )


def _monthly_chart(path: Path, study: FixedExitStudy) -> None:
    rows = [
        row
        for row in study.monthly_rows
        if int(row["horizon_hours"]) == 24 and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    ]
    _bars(
        path,
        [str(row["period"]) for row in rows],
        [float(row["return"]) * 100 for row in rows],
        "24H monthly returns (10 bps)",
        "percent",
        wide=True,
    )


def _trade_distribution(path: Path, study: FixedExitStudy) -> None:
    figure, axis = plt.subplots()
    for horizon in FIXED_HORIZONS:
        values = [
            float(row["net_return"]) * 100
            for row in study.trade_rows
            if int(row["exit_horizon"]) == horizon
            and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
        ]
        axis.hist(values, bins=35, alpha=0.35, label=f"{horizon}H")
    axis.legend()
    axis.set_title("Trade net-return distribution (10 bps)")
    axis.set_xlabel("percent")
    _save(figure, path)


def _mfe_chart(path: Path, study: FixedExitStudy) -> None:
    rows = [
        row
        for row in study.trade_rows
        if int(row["exit_horizon"]) == 24 and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    ]
    figure, axis = plt.subplots()
    axis.scatter(
        [float(row["MFE"]) * 100 for row in rows],
        [float(row["net_return"]) * 100 for row in rows],
        s=9,
        alpha=0.4,
    )
    axis.set_title("24H MFE vs realized net return")
    axis.set_xlabel("MFE percent")
    axis.set_ylabel("net return percent")
    _save(figure, path)


def _holdout_chart(path: Path, study: FixedExitStudy) -> None:
    rows = [
        row for row in study.holdout_rows if int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    ]
    _bars(
        path,
        [f"{row['horizon_hours']}H" for row in rows],
        [float(row["holdout_net_return"]) * 100 for row in rows],
        "Recent 20% holdout (10 bps)",
        "net return percent",
    )


def _baseline_metrics(study: FixedExitStudy) -> list[dict[str, Any]]:
    return [
        row for row in study.metric_rows if int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    ]


def _equity(study: FixedExitStudy, horizon: int) -> list[dict[str, Any]]:
    return [
        row
        for row in study.equity_rows
        if int(row["horizon_hours"]) == horizon
        and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    ]


def _bars(
    path: Path,
    labels: list[str],
    values: list[float],
    title: str,
    ylabel: str,
    *,
    wide: bool = False,
) -> None:
    figure, axis = plt.subplots(figsize=(12, 5) if wide else (7, 4))
    axis.bar(labels, values)
    axis.set_title(title)
    axis.set_ylabel(ylabel)
    if wide:
        axis.tick_params(axis="x", rotation=90)
    _save(figure, path)


def _save(figure: Figure, path: Path) -> None:
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)
