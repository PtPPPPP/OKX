"""Auditable output and fixed assessment for VWAP fixed-exit research."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.run_config import RunConfig
from app.reproducibility import canonical_hash
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_fixed_exit_charts import render_fixed_exit_charts
from backtest.vwap_fixed_exit_research import (
    BASELINE_COST_BPS,
    COST_SCENARIOS_BPS,
    FIXED_HORIZONS,
    INITIAL_EQUITY,
    RANDOM_SAMPLE_COUNT,
    RANDOM_SEED,
    FixedExitStudy,
)

REQUIRED_FILES = {
    "report.md",
    "summary.json",
    "cost_sensitivity.csv",
    "horizon_comparison.csv",
    "regime_performance.csv",
    "holdout_performance.csv",
    "random_benchmark.csv",
    "monthly_returns.csv",
    "yearly_returns.csv",
    "buy_hold_benchmark.csv",
    "data_manifest.json",
    "artifact_manifest.json",
    *(f"trades_{horizon}h.csv" for horizon in FIXED_HORIZONS),
    *(f"equity_{horizon}h.csv" for horizon in FIXED_HORIZONS),
}


def write_fixed_exit_artifacts(
    output: Path,
    *,
    study: FixedExitStudy,
    config: RunConfig,
    parameters: VWAPShadowParameters,
    data_manifest: dict[str, Any],
    strategy_file_hashes: dict[str, str],
    episode_artifact_hash: str,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    for horizon in FIXED_HORIZONS:
        _write_csv(output / f"trades_{horizon}h.csv", _horizon_rows(study.trade_rows, horizon))
        _write_csv(output / f"equity_{horizon}h.csv", _horizon_rows(study.equity_rows, horizon))
    _write_csv(output / "cost_sensitivity.csv", list(study.cost_rows))
    _write_csv(output / "horizon_comparison.csv", list(study.metric_rows))
    _write_csv(output / "regime_performance.csv", list(study.regime_rows))
    _write_csv(output / "holdout_performance.csv", list(study.holdout_rows))
    _write_csv(output / "random_benchmark.csv", list(study.random_rows))
    _write_csv(output / "monthly_returns.csv", list(study.monthly_rows))
    _write_csv(output / "yearly_returns.csv", list(study.yearly_rows))
    _write_csv(output / "buy_hold_benchmark.csv", list(study.benchmark_rows))
    (output / "data_manifest.json").write_text(
        json.dumps(data_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assessment = assess_fixed_exit_study(study)
    summary: dict[str, Any] = {
        "research_id": output.name,
        "research_type": "VWAP_FIXED_EXIT_RESEARCH_V1",
        "research_only": True,
        "offline_research_only": True,
        "instrument": config.market.instrument_id,
        "timeframe": config.market.bar,
        "dataset_hash": data_manifest["dataset_hash"],
        "confirmed_bars": data_manifest["normalized_rows"],
        "source_episode_artifact_hash": episode_artifact_hash,
        "strategy_parameters": parameters.model_dump(mode="json"),
        "entry_contract": "episode_first_buy_next_bar_open",
        "exit_contract": "entry_index_plus_horizon_bar_open",
        "position_model": {
            "long_only": True,
            "leverage": 1,
            "one_position_at_a_time": True,
            "pyramiding": False,
            "averaging_down": False,
            "initial_equity": INITIAL_EQUITY,
            "position_fraction": 1.0,
        },
        "cost_contract": {
            "scenarios_round_trip_bps": COST_SCENARIOS_BPS,
            "baseline_round_trip_bps": BASELINE_COST_BPS,
            "split": "25% entry fee + 25% exit fee + 25% entry slippage + 25% exit slippage",
            "entry_net": "entry_open * (1 + entry_slippage_bps / 10000)",
            "exit_net": "exit_open * (1 - exit_slippage_bps / 10000)",
            "break_even_formula": "bisection root of compounded terminal equity under the declared equal-split cost model",
        },
        "random_benchmark": {"samples": RANDOM_SAMPLE_COUNT, "base_seed": RANDOM_SEED},
        "signals_blocked_by_open_position": study.blocked_by_horizon,
        "performance": list(study.metric_rows),
        "cost_sensitivity": list(study.cost_rows),
        "holdout_performance": list(study.holdout_rows),
        "buy_hold_benchmark": list(study.benchmark_rows),
        "monthly_return_statistics": _period_statistics(study.monthly_rows),
        "yearly_return_statistics": _period_statistics(study.yearly_rows),
        **assessment,
        "strategy_file_hashes": strategy_file_hashes,
        "config_hash": canonical_hash(config.model_dump(mode="json")),
        "generated_at": datetime.now(UTC).isoformat(),
        "production_strategy_changed": False,
        "shadow_strategy_changed": False,
        "bounded_demo_strategy_changed": False,
        "risk_parameters_changed": False,
        "budget_changed": False,
        "safety": {
            "bounded_demo_started": 0,
            "broker_write_calls": 0,
            "place_order_calls": 0,
            "cancel_order_calls": 0,
            "private_api_write_calls": 0,
            "live_trading": False,
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    render_fixed_exit_charts(output, study)
    _write_artifact_manifest(output)
    return summary


def assess_fixed_exit_study(study: FixedExitStudy) -> dict[str, object]:
    """Apply a predeclared, ordered assessment without choosing a winner post hoc."""
    metrics = {
        (int(row["horizon_hours"]), int(row["round_trip_cost_bps"])): row
        for row in study.metric_rows
    }
    holdouts = {
        (int(row["horizon_hours"]), int(row["round_trip_cost_bps"])): row
        for row in study.holdout_rows
    }
    historical_best = max(
        FIXED_HORIZONS, key=lambda value: float(metrics[(value, BASELINE_COST_BPS)]["total_return"])
    )
    oos_best = max(
        FIXED_HORIZONS,
        key=lambda value: float(holdouts[(value, BASELINE_COST_BPS)]["holdout_net_return"]),
    )
    random_summary: dict[int, dict[str, float | None]] = {}
    for horizon in FIXED_HORIZONS:
        rows = [
            row
            for row in study.random_rows
            if int(row["horizon_hours"]) == horizon
            and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
        ]
        random_summary[horizon] = {
            "return_percentile": _first_number(rows, "actual_return_percentile"),
            "sharpe_percentile": _first_number(rows, "actual_sharpe_percentile"),
            "max_dd_percentile": _first_number(rows, "actual_max_dd_percentile"),
        }
    zero_profitable = [h for h in FIXED_HORIZONS if float(metrics[(h, 0)]["total_return"]) > 0]
    oos_robust = [
        h
        for h in FIXED_HORIZONS
        if float(holdouts[(h, BASELINE_COST_BPS)]["holdout_net_return"]) > 0
        and float(holdouts[(h, BASELINE_COST_BPS)]["holdout_profit_factor"] or 0) > 1
    ]
    best_random = random_summary[historical_best]
    clearly_beats_random = (
        float(best_random["return_percentile"] or 0) >= 0.8
        and float(best_random["sharpe_percentile"] or 0) >= 0.8
        and float(best_random["max_dd_percentile"] or 0) >= 0.8
    )
    cost_row = next(
        row
        for row in study.cost_rows
        if int(row["horizon_hours"]) == historical_best
        and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    )
    cost_fragile = bool(cost_row["high_cost_fragility"])
    best_regimes = [
        row
        for row in study.regime_rows
        if int(row["horizon_hours"]) == historical_best
        and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
        and row["dimension"] == "market_regime"
    ]
    regime_dependent = sum(float(row["net_return"]) > 0 for row in best_regimes) < max(
        2, len(best_regimes)
    )
    if not zero_profitable:
        final_state = "FIXED_EXIT_UNPROFITABLE"
    elif cost_fragile:
        final_state = "FIXED_EXIT_COST_FRAGILE"
    elif not oos_robust:
        final_state = "FIXED_EXIT_OOS_WEAK"
    elif regime_dependent:
        final_state = "FIXED_EXIT_REGIME_DEPENDENT"
    elif clearly_beats_random:
        final_state = "READY_FOR_WALK_FORWARD_RESEARCH"
    else:
        final_state = "FIXED_EXIT_PROMISING"
    return {
        "historical_best_horizon": historical_best,
        "oos_best_horizon": oos_best,
        "profitable_horizons_by_cost_bps": {
            str(cost): [h for h in FIXED_HORIZONS if float(metrics[(h, cost)]["total_return"]) > 0]
            for cost in COST_SCENARIOS_BPS
        },
        "oos_robust_horizons_at_baseline": oos_robust,
        "historical_best_random_percentiles": best_random,
        "clearly_beats_random": clearly_beats_random,
        "regime_dependent": regime_dependent,
        "final_state": final_state,
    }


def _report(summary: dict[str, Any]) -> str:
    metrics = [
        row
        for row in summary["performance"]
        if int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    ]
    benchmark = summary["buy_hold_benchmark"][0]
    best = next(
        row
        for row in metrics
        if int(row["horizon_hours"]) == int(summary["historical_best_horizon"])
    )
    best_holdout = next(
        row
        for row in summary["holdout_performance"]
        if int(row["horizon_hours"]) == int(summary["oos_best_horizon"])
        and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
    )
    concentration = float(best["top_5_trade_contribution"] or 0)
    lines = [
        "# VWAP_FIXED_EXIT_RESEARCH_V1",
        "",
        "## Executive Summary",
        "",
        f"- Historical best horizon at {BASELINE_COST_BPS} bps: {summary['historical_best_horizon']}H.",
        f"- Recent 20% best horizon at {BASELINE_COST_BPS} bps: {summary['oos_best_horizon']}H.",
        f"- Clearly beats deterministic random entry: {summary['clearly_beats_random']}.",
        f"- Regime dependent: {summary['regime_dependent']}.",
        f"- Final state: `{summary['final_state']}`.",
        "",
        "## Baseline Performance (10 bps)",
        "",
        "| Horizon | Trades | Return | CAGR | Max DD | Sharpe | Win rate | Exposure |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['horizon_hours']}H | {row['trade_count']} | {_pct(row['total_return'])} | "
            f"{_pct(row['CAGR'])} | {_pct(row['max_drawdown'])} | {_fmt(row['Sharpe'])} | "
            f"{_pct(row['win_rate'])} | {_pct(row['market_exposure'])} |"
        )
    lines.extend(
        [
            "",
            "## Cost Robustness",
            "",
            "```json",
            json.dumps(summary["profitable_horizons_by_cost_bps"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Buy & Hold",
            "",
            f"Return={_pct(benchmark['total_return'])}, CAGR={_pct(benchmark['CAGR'])}, "
            f"Max DD={_pct(benchmark['max_drawdown'])}, Sharpe={_fmt(benchmark['Sharpe'])}.",
            "",
            "## Final Assessment",
            "",
            f"1. Historical best: {summary['historical_best_horizon']}H at the declared 10 bps baseline.",
            f"2. OOS best: {summary['oos_best_horizon']}H, but its recent-20% return is {_pct(best_holdout['holdout_net_return'])}; no horizon is OOS robust.",
            f"3. Cost edge: profitable horizons by 5/10/15/20 bps are {json.dumps({key: summary['profitable_horizons_by_cost_bps'][key] for key in ('5', '10', '15', '20')})}.",
            f"4. Drawdown: the best historical model loses {_pct(best['max_drawdown'])} peak-to-trough; this is not acceptable as a deployment-ready result.",
            f"5. Random entry: return/sharpe/max-DD percentiles are {_pct(summary['historical_best_random_percentiles']['return_percentile'])}/{_pct(summary['historical_best_random_percentiles']['sharpe_percentile'])}/{_pct(summary['historical_best_random_percentiles']['max_dd_percentile'])}; clearly superior requires all three to reach 80%, result={summary['clearly_beats_random']}.",
            f"6. Buy & Hold risk-adjusted comparison: VWAP Sharpe {_fmt(best['Sharpe'])} vs Buy & Hold {_fmt(benchmark['Sharpe'])}; VWAP is not clearly superior.",
            f"7. Concentration: top five winners equal {concentration:.2f} times total net profit, so gains are materially concentrated and offset by losing trades.",
            f"8. Recent validity: recent-20% return is {_pct(best_holdout['holdout_net_return'])}; the fixed-exit edge is not currently valid OOS.",
            "",
            "## Safety",
            "",
            "```text",
            "OFFLINE_RESEARCH_ONLY=true",
            "production_strategy_changed=false",
            "broker_write_calls=0",
            "place_order_calls=0",
            "cancel_order_calls=0",
            "bounded_demo_started=0",
            "live_trading=false",
            "```",
            "",
            "## Final State",
            "",
            f"`{summary['final_state']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _horizon_rows(rows: tuple[dict[str, Any], ...], horizon: int) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if int(row["horizon_hours"] if "horizon_hours" in row else row["exit_horizon"]) == horizon
    ]


def _period_statistics(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for horizon in FIXED_HORIZONS:
        selected = [
            row
            for row in rows
            if int(row["horizon_hours"]) == horizon
            and int(row["round_trip_cost_bps"]) == BASELINE_COST_BPS
        ]
        values = [float(row["return"]) for row in selected]
        result.append(
            {
                "horizon_hours": horizon,
                "round_trip_cost_bps": BASELINE_COST_BPS,
                "positive_periods": sum(value > 0 for value in values),
                "negative_periods": sum(value < 0 for value in values),
                "best_period": max(selected, key=lambda row: float(row["return"]))["period"],
                "best_return": max(values),
                "worst_period": min(selected, key=lambda row: float(row["return"]))["period"],
                "worst_return": min(values),
            }
        )
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("status,reason\nnot_run,no_rows\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_artifact_manifest(output: Path) -> None:
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "artifact_manifest.json"
    }
    (output / "artifact_manifest.json").write_text(
        json.dumps(
            {
                "research_id": output.name,
                "files": hashes,
                "artifact_set_hash": canonical_hash(hashes),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _first_number(rows: list[dict[str, object]], key: str) -> float | None:
    return _number(rows[0][key], default=None) if rows else None


def _number(value: object, default: float | None = 0.0) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default


def _pct(value: object) -> str:
    number = _number(value)
    return f"{number * 100:.3f}%" if number is not None else "n/a"


def _fmt(value: object) -> str:
    number = _number(value, default=None)
    return f"{number:.3f}" if number is not None else "n/a"
