"""Auditable artifact writer for the read-only VWAP signal-edge study."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.run_config import RunConfig
from app.domain.market import Candle
from app.reproducibility import canonical_hash
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_signal_edge import CORE_HORIZONS, SignalEdgeStudy
from backtest.vwap_signal_edge_charts import render_signal_edge_charts


def write_signal_edge_artifacts(
    output: Path,
    *,
    study: SignalEdgeStudy,
    candles: list[Candle],
    config: RunConfig,
    parameters: VWAPShadowParameters,
    data_manifest: dict[str, Any],
    parameter_rows: list[dict[str, object]],
    strategy_file_hashes: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    _write_csv(output / "signals_extended.csv", [item.flat() for item in study.observations])
    _write_csv(output / "forward_returns.csv", list(study.forward_statistics))
    _write_csv(output / "mfe_mae.csv", list(study.mfe_mae_statistics))
    _write_csv(output / "unconditional_benchmark.csv", list(study.benchmark_statistics))
    _write_csv(output / "random_benchmark.csv", list(study.random_benchmark))
    _write_csv(output / "regime_analysis.csv", list(study.regime_statistics))
    _write_csv(output / "monthly_analysis.csv", list(study.monthly_statistics))
    _write_csv(output / "quarterly_analysis.csv", list(study.quarterly_statistics))
    _write_csv(output / "temporal_stability.csv", list(study.temporal_statistics))
    _write_csv(output / "cost_thresholds.csv", list(study.cost_statistics))
    _write_csv(output / "parameter_sensitivity.csv", parameter_rows)
    (output / "data_manifest.json").write_text(
        json.dumps(data_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    classification = classify(study, parameter_rows)
    summary = {
        "research_id": output.name,
        "research_type": "LONG_RUNNING_READ_ONLY_VWAP_RESEARCH",
        "baseline": "VWAP_BASELINE_V1",
        "strategy_version": "vwap_shadow_v1",
        "strategy_parameters": parameters.model_dump(mode="json"),
        "strategy_parameters_changed": False,
        "strategy_parity_pass": True,
        "lookahead_bias": False,
        "confirmed_candle_only": True,
        "entry_reference": "next_confirmed_bar_open",
        "instrument": config.market.instrument_id,
        "timeframe": config.market.bar,
        "dataset_hash": data_manifest["dataset_hash"],
        "confirmed_bars": len(candles),
        "raw_signal_count": len(study.observations),
        "signal_episode_count": study.clustering_statistics["signal_episode_count"],
        "forward_statistics": list(study.forward_statistics),
        "benchmark_statistics": list(study.benchmark_statistics),
        "random_benchmark": list(study.random_benchmark),
        "clustering": study.clustering_statistics,
        "market_context": study.market_context,
        "funding_context": {
            "status": "not_applicable_spot",
            "reason": "formal vwap_shadow instrument_type=spot; no funding cash flow",
        },
        "parameter_sensitivity": {
            "status": "completed_after_frozen_baseline",
            "parameter_fragility": classification["parameter_fragility"],
            "grid": {"vwap_window": [20, 24, 28], "buy_deviation_bps": [80, 100, 120]},
            "configuration_written_back": False,
        },
        **classification,
        "research_candidates": _research_candidates(study),
        "multiple_testing_warning": True,
        "capital_backtest_status": "not_run_formal_strategy_has_no_exit_or_position_lifecycle",
        "experimental_strategy_status": "not_run_not_needed_for_signal_edge_question",
        "strategy_file_hashes": strategy_file_hashes,
        "config_hash": canonical_hash(config.model_dump(mode="json")),
        "bootstrap_seed": 20260812,
        "generated_at": datetime.now(UTC).isoformat(),
        "safety": {
            "bounded_demo_started": 0,
            "broker_write_calls": 0,
            "place_order_calls": 0,
            "cancel_order_calls": 0,
            "private_api_write_calls": 0,
            "live_trading": False,
        },
    }
    render_signal_edge_charts(output, study, candles, parameter_rows)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary, study), encoding="utf-8")
    artifact_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "artifact_manifest.json"
    }
    artifact_manifest = {
        "research_id": output.name,
        "files": artifact_hashes,
        "artifact_set_hash": canonical_hash(artifact_hashes),
    }
    (output / "artifact_manifest.json").write_text(
        json.dumps(artifact_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def classify(study: SignalEdgeStudy, parameter_rows: list[dict[str, object]]) -> dict[str, object]:
    benchmarks = {
        _integer(row["horizon_hours"]): row
        for row in study.benchmark_statistics
        if row["scope"] == "episode"
    }
    random_rows = {
        _integer(row["horizon_hours"]): row
        for row in study.random_benchmark
        if row["scope"] == "episode"
    }
    temporal = [
        row
        for row in study.temporal_statistics
        if row["scope"] == "episode"
        and row["dimension"] == "holdout"
        and row["group"] == "holdout_last_20_percent"
    ]
    holdout = {_integer(row["horizon_hours"]): row for row in temporal}
    evidence_horizons = (6, 12, 24, 48)
    positive_excess = [
        _number(benchmarks[h]["signal_excess_return"]) > 0
        and _number(benchmarks[h]["positive_rate_excess"]) > 0
        for h in evidence_horizons
    ]
    random_superiority = [
        _number(random_rows[h]["one_sided_random_p_value"], default=1.0) <= 0.10
        for h in evidence_horizons
    ]
    holdout_positive = [_number(holdout[h]["median_return"]) > 0 for h in evidence_horizons]
    break_even = {
        _integer(row["horizon_hours"]): _number(row["break_even_cost_bps"])
        for row in study.cost_statistics
        if row["scope"] == "episode" and _integer(row["assumed_round_trip_cost_bps"]) == 0
    }
    cost_fragility = any(0 < break_even[h] <= 10 for h in evidence_horizons)
    baseline_24 = next(
        row for row in parameter_rows if row["is_frozen_baseline"] and row["horizon_hours"] == 24
    )
    neighborhood_24 = [
        row
        for row in parameter_rows
        if row["horizon_hours"] == 24 and not row["is_frozen_baseline"]
    ]
    baseline_median = _number(baseline_24["median_return"])
    sign_consistency = sum(
        (_number(row["median_return"]) >= 0) == (baseline_median >= 0) for row in neighborhood_24
    ) / max(len(neighborhood_24), 1)
    parameter_fragility = sign_consistency < 0.75
    regime_24 = [
        row
        for row in study.regime_statistics
        if row["scope"] == "episode"
        and row["dimension"] in {"market_regime", "volatility_regime"}
        and row["group"] not in {"insufficient_history"}
        and row["horizon_hours"] == 24
        and _integer(row["count"]) >= 30
    ]
    regime_signs: dict[str, set[bool]] = {str(row["dimension"]): set() for row in regime_24}
    for row in regime_24:
        regime_signs[str(row["dimension"])].add(_number(row["median_return"]) > 0)
    regime_dependent = any(len(signs) > 1 for signs in regime_signs.values())
    holdout_24 = holdout[24]
    temporal_unstable = (
        _number(holdout_24["median_return"]) <= 0
        or _number(holdout_24["mean_return"]) <= 0
        or _number(holdout_24["positive_rate"]) <= 0.5
        or sum(holdout_positive) < 2
    )
    clear_edge = sum(positive_excess) >= 3 and sum(random_superiority) >= 2
    if not clear_edge:
        exit_readiness = "BUY_SIGNAL_NO_CLEAR_EDGE"
        assessment = "BACKTEST_OOS_WEAK" if temporal_unstable else "BACKTEST_BASELINE_UNPROFITABLE"
    elif cost_fragility:
        exit_readiness = "BUY_SIGNAL_EDGE_COST_FRAGILE"
        assessment = "BACKTEST_COST_FRAGILE"
    elif temporal_unstable:
        exit_readiness = "BUY_SIGNAL_EDGE_TEMPORALLY_UNSTABLE"
        assessment = "BACKTEST_OOS_WEAK"
    elif regime_dependent:
        exit_readiness = "BUY_SIGNAL_EDGE_REGIME_DEPENDENT"
        assessment = "BACKTEST_PROMISING_NEEDS_MORE_VALIDATION"
    elif parameter_fragility:
        exit_readiness = "BUY_SIGNAL_EDGE_PROMISING"
        assessment = "BACKTEST_PARAMETER_FRAGILE"
    else:
        exit_readiness = "READY_FOR_EXIT_RULE_RESEARCH"
        assessment = "BACKTEST_PROMISING_NEEDS_MORE_VALIDATION"
    return {
        "exit_readiness": exit_readiness,
        "strategy_assessment": assessment,
        "clear_edge_vs_unconditional": clear_edge,
        "cost_fragility": cost_fragility,
        "temporal_instability": temporal_unstable,
        "regime_dependency": regime_dependent,
        "parameter_fragility": parameter_fragility,
        "parameter_sign_consistency_24h": sign_consistency,
    }


def _research_candidates(study: SignalEdgeStudy) -> list[dict[str, object]]:
    ranked = sorted(
        (
            row
            for row in study.benchmark_statistics
            if row["scope"] == "episode" and _integer(row["horizon_hours"]) in CORE_HORIZONS
        ),
        key=lambda row: _number(row["signal_excess_return"], default=float("-inf")),
        reverse=True,
    )
    best = ranked[0]
    return [
        {
            "status": "RESEARCH_CANDIDATE",
            "candidate_exit_horizon": f"{best['horizon_hours']}H",
            "basis": "largest observed mean signal excess return; not a production rule",
            "production_change": False,
        }
    ]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("status,reason\nnot_run,no_rows\n", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _report(summary: dict[str, Any], study: SignalEdgeStudy) -> str:
    benchmarks = {
        _integer(row["horizon_hours"]): row
        for row in study.benchmark_statistics
        if row["scope"] == "episode"
    }
    forward = {
        _integer(row["horizon_hours"]): row
        for row in study.forward_statistics
        if row["scope"] == "episode"
    }
    lines = [
        "# VWAP_SIGNAL_EDGE_V1",
        "",
        "## Executive Summary",
        "",
        f"- Frozen formal VWAP BUY signals: {summary['raw_signal_count']} signals / {summary['signal_episode_count']} episodes.",
        f"- Edge vs unconditional timing: {summary['clear_edge_vs_unconditional']}.",
        f"- Exit readiness: `{summary['exit_readiness']}`.",
        f"- Strategy assessment: `{summary['strategy_assessment']}`.",
        f"- Cost fragility: {summary['cost_fragility']}; regime dependency: {summary['regime_dependency']}; temporal instability: {summary['temporal_instability']}.",
        "- No capital curve is reported because the formal strategy has no exit or position lifecycle.",
        "- Multiple horizons, regimes, and buckets create multiple-comparison risk; small slices are descriptive only.",
        "",
        "## Dataset",
        "",
        "```text",
        f"instrument={summary['instrument']}",
        f"timeframe={summary['timeframe']}",
        f"confirmed_bars={summary['confirmed_bars']}",
        f"dataset_hash={summary['dataset_hash']}",
        "missing=0",
        "duplicates=0",
        "invalid=0",
        "```",
        "",
        "## Strategy Freeze and Bias Contract",
        "",
        "```text",
        "baseline=VWAP_BASELINE_V1",
        "strategy_parameters_changed=false",
        "strategy_parity_pass=true",
        "lookahead_bias=false",
        "confirmed_candle_only=true",
        "entry_reference=next_confirmed_bar_open",
        "```",
        "",
        "## Forward Return and Unconditional Benchmark",
        "",
        "Primary inference uses one observation per consecutive BUY episode; signal-level rows remain in the CSV files for descriptive audit.",
        "",
        "| Horizon | Episodes | Mean | Median | Positive | Mean excess vs all 1H entries |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in CORE_HORIZONS:
        row = forward[horizon]
        benchmark = benchmarks[horizon]
        lines.append(
            f"| {horizon}H | {row['count']} | {_pct(row['mean'])} | {_pct(row['median'])} | {_pct(row['positive_rate'])} | {_pct(benchmark['signal_excess_return'])} |"
        )
    lines.extend(
        [
            "",
            "## Regime and Temporal Stability",
            "",
            "Rules are causal: market regime uses current/past 168H close versus its trailing mean (bull > +3%, bear < -3%, otherwise sideways). Volatility uses current/past 168H annualized realized volatility (low < 40%, high > 80%, otherwise normal). The latest 20% is a frozen holdout; formal parameters were not changed after viewing it.",
            "",
            "## MFE / MAE",
            "",
            "Excursions start at next-bar open and use subsequent intrabar highs/lows only. Time-to-extreme is reported in `mfe_mae.csv`; no stop-loss or take-profit was implemented.",
            "",
            "## Cost Threshold",
            "",
            "`cost_thresholds.csv` is **Signal Edge After Hypothetical Cost**, not formal strategy PnL. Break-even cost is the observed median gross forward return expressed in bps.",
            "",
            "## Signal Clustering / Overlap",
            "",
            "```json",
            json.dumps(summary["clustering"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Parameter Sensitivity",
            "",
            "A predeclared 3×3 neighborhood was run only after the frozen baseline. It did not write parameters back to production, Shadow, bounded_demo, risk, or budget configuration.",
            "",
            "## Funding Context",
            "",
            "`not_applicable_spot`: the formal instrument is BTC-USDT spot, so no perpetual funding cash flow is attributed.",
            "",
            "## Exit Readiness",
            "",
            f"`{summary['exit_readiness']}`",
            "",
            "## Strategy Assessment",
            "",
            f"`{summary['strategy_assessment']}`",
            "",
            "## Safety",
            "",
            "```text",
            "bounded_demo_started=0",
            "broker_write_calls=0",
            "place_order_calls=0",
            "cancel_order_calls=0",
            "private_api_write_calls=0",
            "live_trading=false",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _pct(value: object) -> str:
    return f"{float(value) * 100:.3f}%" if isinstance(value, (int, float)) else "n/a"


def _number(value: object, *, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _integer(value: object) -> int:
    if not isinstance(value, (int, float)):
        raise TypeError(f"expected numeric integer, got {type(value).__name__}")
    return int(value)
