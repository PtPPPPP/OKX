"""Auditable artifacts and fixed assessment for VWAP walk-forward research."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from app.config.run_config import RunConfig
from app.reproducibility import canonical_hash
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_walk_forward_charts import render_walk_forward_charts
from backtest.vwap_walk_forward_research import WalkForwardStudy


def write_walk_forward_artifacts(
    output: Path,
    *,
    study: WalkForwardStudy,
    config: RunConfig,
    parameters: VWAPShadowParameters,
    data_manifest: dict[str, Any],
    source_artifact_hashes: dict[str, str],
    strategy_file_hashes: dict[str, str],
    fixed_exit_context: dict[str, Any],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    _write_csv(output / "walk_forward_windows.csv", list(study.window_rows))
    _write_csv(output / "candidate_comparison.csv", list(study.candidate_rows))
    _write_csv(output / "stitched_oos_equity.csv", list(study.stitched_rows))
    _write_csv(output / "final_holdout.csv", list(study.holdout_rows))
    _write_csv(output / "regime_filter_results.csv", list(study.regime_rows))
    _write_csv(output / "cost_stress_results.csv", list(study.cost_rows))
    _write_csv(output / "benchmark_results.csv", list(study.benchmark_rows))
    _write_csv(output / "parameter_robustness.csv", list(study.robustness_rows))
    _write_csv(output / "bootstrap_results.csv", list(study.bootstrap_rows))
    _write_csv(output / "yearly_oos_performance.csv", list(study.year_rows))
    concentration_rows = [
        *fixed_exit_context["concentration_rows"],
        *study.concentration_rows,
    ]
    _write_csv(output / "profit_concentration.csv", concentration_rows)
    _write_csv(output / "profit_by_period.csv", fixed_exit_context["profit_source_rows"])
    assessment = assess_walk_forward(study, fixed_exit_context)
    summary: dict[str, Any] = {
        "research_id": output.name,
        "research_type": "VWAP_WALK_FORWARD_RESEARCH_V1",
        "research_only": True,
        "offline_research_only": True,
        "instrument": config.market.instrument_id,
        "timeframe": config.market.bar,
        "dataset_hash": data_manifest["dataset_hash"],
        "confirmed_bars": data_manifest["normalized_rows"],
        "source_artifact_hashes": source_artifact_hashes,
        "strategy_parameters": parameters.model_dump(mode="json"),
        "candidate_count": len(study.candidate_definitions),
        "candidate_definitions": study.candidate_definitions,
        "candidate_selection_reason": (
            "24H is the frozen historical candidate; 48H is the surviving horizon control; "
            "four 24H filters are predeclared causal diagnostics"
        ),
        "multiple_testing_risk": (
            "Six candidates share the same development OOS windows; no candidate was added "
            "after viewing test or holdout results"
        ),
        "walk_forward_contract": {
            "train_months": 12,
            "test_months": 3,
            "step_months": 3,
            "random_split": False,
            "test_windows_total": len(study.windows),
            "regime_threshold_fit": "train-only causal 168H volatility quantiles",
            "final_holdout_fraction": 0.20,
            "final_holdout_start": study.holdout_start,
            "final_holdout_execution_count": study.holdout_execution_count,
            "holdout_not_used_for_current_candidate_design_or_selection": True,
            "holdout_globally_pristine": False,
            "holdout_prior_exposure_note": (
                "The preceding fixed-exit study already reported the recent 20% segment; "
                "this study preserves isolation from current candidate design but cannot "
                "claim a globally unseen dataset segment"
            ),
        },
        "cost_scenarios_bps": [10, 20],
        "candidate_comparison": study.candidate_rows,
        "final_holdout": study.holdout_rows,
        "bootstrap": study.bootstrap_rows,
        "fixed_exit_profit_context": fixed_exit_context,
        **assessment,
        "generated_at": datetime.now(UTC).isoformat(),
        "config_hash": canonical_hash(config.model_dump(mode="json")),
        "strategy_file_hashes": strategy_file_hashes,
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
    (output / "data_manifest.json").write_text(
        json.dumps(data_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    render_walk_forward_charts(output, study)
    _write_manifest(output)
    return summary


def assess_walk_forward(
    study: WalkForwardStudy, fixed_exit_context: dict[str, Any]
) -> dict[str, Any]:
    rows = {
        (str(row["candidate_id"]), int(row["round_trip_cost_bps"])): row
        for row in study.candidate_rows
    }
    holdout = {
        (str(row["candidate_id"]), int(row["round_trip_cost_bps"])): row
        for row in study.holdout_rows
    }
    primary = rows[("h24_unfiltered", 10)]
    primary_stress = rows[("h24_unfiltered", 20)]
    primary_holdout = holdout[("h24_unfiltered", 10)]
    normal = rows[("h24_normal_vol", 10)]
    normal_holdout = holdout[("h24_normal_vol", 10)]
    unfiltered_count = int(primary["stitched_oos_trade_count"])
    normal_count = int(normal["stitched_oos_trade_count"])
    normal_trade_reduction = 1 - normal_count / unfiltered_count if unfiltered_count else 1.0
    normal_filter_improves = (
        float(normal["stitched_oos_total_return"]) > float(primary["stitched_oos_total_return"])
        and float(normal["positive_window_ratio"]) >= float(primary["positive_window_ratio"])
        and float(normal_holdout["total_return"]) > float(primary_holdout["total_return"])
    )
    thresholds = [
        row
        for row in study.robustness_rows
        if row.get("check") == "volatility threshold sensitivity"
    ]
    regime_filter_fragile = bool(
        thresholds
        and (
            max(float(row["stitched_oos_total_return"]) for row in thresholds)
            - min(float(row["stitched_oos_total_return"]) for row in thresholds)
            > 0.20
        )
    )
    bootstrap = next(row for row in study.bootstrap_rows if row["candidate_id"] == "h24_unfiltered")
    random_percentiles = [
        float(row["random_entry_percentile"])
        for row in study.window_rows
        if row["candidate_id"] == "h24_unfiltered"
        and int(row["round_trip_cost_bps"]) == 10
        and row["random_entry_percentile"] is not None
    ]
    clearly_beats_random = bool(random_percentiles and median(random_percentiles) >= 0.80)
    years = [row for row in study.year_rows if row["candidate_id"] == "h24_unfiltered"]
    structural_decay = bool(
        years and float(years[-1]["net_return"]) < 0 and float(primary_holdout["total_return"]) < 0
    )
    majority_positive = float(primary["positive_window_ratio"]) > 0.5
    stress_survives = float(primary_stress["stitched_oos_total_return"]) > 0
    holdout_positive = float(primary_holdout["total_return"]) > 0
    reasonable_drawdown = float(primary["stitched_oos_max_drawdown"]) > -0.30
    candidate_ready = all(
        (
            majority_positive,
            holdout_positive,
            stress_survives,
            clearly_beats_random,
            reasonable_drawdown,
            not structural_decay,
            int(primary["stitched_oos_trade_count"]) >= 100,
        )
    )
    if float(primary["stitched_oos_total_return"]) <= 0:
        final_state = "WALK_FORWARD_UNPROFITABLE"
    elif not stress_survives:
        final_state = "WALK_FORWARD_COST_FRAGILE"
    elif regime_filter_fragile:
        final_state = "WALK_FORWARD_REGIME_FRAGILE"
    elif structural_decay:
        final_state = "WALK_FORWARD_TEMPORALLY_UNSTABLE"
    elif not holdout_positive or not majority_positive:
        final_state = "WALK_FORWARD_OOS_WEAK"
    elif candidate_ready:
        final_state = "RESEARCH_CANDIDATE_V2_READY_FOR_HUMAN_REVIEW"
    else:
        final_state = "WALK_FORWARD_PROMISING"
    pure_vwap_stop = final_state != "RESEARCH_CANDIDATE_V2_READY_FOR_HUMAN_REVIEW"
    full_sample_removal = {
        int(row["removed_top_winners"]): row for row in fixed_exit_context["concentration_rows"]
    }
    oos_removal = {int(row["removed_top_winners"]): row for row in study.concentration_rows}
    return {
        "primary_candidate": "h24_unfiltered",
        "primary_walk_forward": primary,
        "primary_stress": primary_stress,
        "primary_final_holdout": primary_holdout,
        "normal_vol_filter_improves_oos": normal_filter_improves,
        "normal_vol_trade_count_reduction": normal_trade_reduction,
        "regime_filter_fragile": regime_filter_fragile,
        "median_random_entry_percentile": median(random_percentiles),
        "clearly_beats_random": clearly_beats_random,
        "structural_edge_decay": structural_decay,
        "primary_bootstrap": bootstrap,
        "candidate_v2_ready": candidate_ready,
        "top_full_sample_profit_periods": fixed_exit_context["top_profit_periods"],
        "full_sample_top5_removed": full_sample_removal[5],
        "full_sample_top10_removed": full_sample_removal[10],
        "oos_top5_removed": oos_removal[5],
        "oos_top10_removed": oos_removal[10],
        "PURE_VWAP_RESEARCH_STOP_RECOMMENDED": pure_vwap_stop,
        "final_state": final_state,
    }


def _report(summary: dict[str, Any]) -> str:
    primary = summary["primary_walk_forward"]
    holdout = summary["primary_final_holdout"]
    stress = summary["primary_stress"]
    stable = (
        float(primary["positive_window_ratio"]) > 0.5
        and float(primary["stitched_oos_total_return"]) > 0
    )
    top_periods = ", ".join(
        f"{row['period']} (net PnL={float(row['net_pnl']):.2f})"
        for row in summary["top_full_sample_profit_periods"][:3]
    )
    lines = [
        "# VWAP_WALK_FORWARD_RESEARCH_V1",
        "",
        "## Executive Summary",
        "",
        "- Primary candidate: 24H unfiltered, frozen before this study.",
        f"- OOS windows positive: {primary['positive_test_windows']}/{primary['test_windows_total']}.",
        f"- Stitched OOS return: {_pct(primary['stitched_oos_total_return'])}.",
        f"- Final untouched holdout return: {_pct(holdout['total_return'])}.",
        f"- Final state: `{summary['final_state']}`.",
        "",
        "## Direct Answers",
        "",
        f"1. Structural edge decay: {summary['structural_edge_decay']}.",
        f"2. Main full-sample profit periods: {top_periods}.",
        f"3. Full-sample return after removing top 5/10 winners: {_pct(summary['full_sample_top5_removed']['total_return_after_removal'])} / {_pct(summary['full_sample_top10_removed']['total_return_after_removal'])}; stitched OOS: {_pct(summary['oos_top5_removed']['total_return_after_removal'])} / {_pct(summary['oos_top10_removed']['total_return_after_removal'])}.",
        f"4. Normal-vol filter improves strict next-window OOS stability: {summary['normal_vol_filter_improves_oos']}; fragility flag={summary['regime_filter_fragile']}.",
        f"5. Stitched OOS positive: {float(primary['stitched_oos_total_return']) > 0}, return={_pct(primary['stitched_oos_total_return'])}, stable windows={stable}.",
        f"6. Final untouched holdout: trades={holdout['trade_count']}, return={_pct(holdout['total_return'])}, Sharpe={_fmt(holdout['Sharpe'])}, max DD={_pct(holdout['max_drawdown'])}.",
        f"7. Stress cost survives: {float(stress['stitched_oos_total_return']) > 0}, return={_pct(stress['stitched_oos_total_return'])}.",
        f"8. Sufficient evidence to continue pure VWAP entry research: {not summary['PURE_VWAP_RESEARCH_STOP_RECOMMENDED']}.",
        "",
        "## Original Research Gates",
        "",
        f"- 24H remains frozen, but promotion is authorized: {summary['candidate_v2_ready']}.",
        f"- Normal-vol trade-count reduction: {_pct(summary['normal_vol_trade_count_reduction'])}.",
        f"- Clearly beats random timing: {summary['clearly_beats_random']}, median percentile={_pct(summary['median_random_entry_percentile'])}.",
        f"- Worth entering Shadow V2: {summary['candidate_v2_ready']}; human review remains mandatory.",
        f"- PURE_VWAP_RESEARCH_STOP_RECOMMENDED={str(summary['PURE_VWAP_RESEARCH_STOP_RECOMMENDED']).lower()}.",
        "",
        "## Safety",
        "",
        "```text",
        "OFFLINE_RESEARCH_ONLY=true",
        "production_strategy_changed=false",
        "bounded_demo_started=0",
        "broker_write_calls=0",
        "place_order_calls=0",
        "cancel_order_calls=0",
        "live_trading=false",
        "```",
        "",
        "## Final State",
        "",
        f"`{summary['final_state']}`",
    ]
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("status,reason\nnot_run,no_rows\n", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_manifest(output: Path) -> None:
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


def _pct(value: object) -> str:
    return f"{float(value) * 100:.3f}%" if isinstance(value, (int, float)) else "n/a"


def _fmt(value: object) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) else "n/a"
