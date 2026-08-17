from __future__ import annotations

import json
from pathlib import Path

from app.config.run_config import load_run_config
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_walk_forward_artifacts import write_walk_forward_artifacts
from backtest.vwap_walk_forward_research import WalkForwardStudy


def _study() -> WalkForwardStudy:
    base = {
        "candidate_id": "h24_unfiltered",
        "candidate_role": "primary_frozen_candidate",
        "horizon_hours": 24,
        "volatility_filter": "none",
        "trend_filter": "none",
        "test_windows_total": 2,
        "positive_test_windows": 1,
        "negative_test_windows": 1,
        "positive_window_ratio": 0.5,
        "median_test_return": 0.0,
        "median_profit_factor": 1.0,
        "median_sharpe": 0.0,
        "worst_test_return": -0.1,
        "worst_max_drawdown": -0.2,
        "stitched_oos_total_return": 0.1,
        "stitched_oos_max_drawdown": -0.2,
        "stitched_oos_sharpe": 0.2,
        "stitched_oos_profit_factor": 1.1,
        "stitched_oos_win_rate": 0.51,
        "stitched_oos_trade_count": 100,
        "average_trade": 0.001,
    }
    candidate_rows = []
    holdout_rows = []
    definitions = (
        {"candidate_id": "h24_unfiltered", "horizon_hours": 24},
        {"candidate_id": "h24_normal_vol", "horizon_hours": 24},
    )
    for candidate in ("h24_unfiltered", "h24_normal_vol"):
        for cost in (10, 20):
            candidate_rows.append({**base, "candidate_id": candidate, "round_trip_cost_bps": cost})
            holdout_rows.append(
                {
                    "candidate_id": candidate,
                    "round_trip_cost_bps": cost,
                    "trade_count": 20,
                    "total_return": -0.05,
                    "Sharpe": -0.2,
                    "profit_factor": 0.8,
                    "win_rate": 0.45,
                    "max_drawdown": -0.1,
                }
            )
    return WalkForwardStudy(
        windows=(),
        window_rows=(
            {
                "window_id": "WF01",
                "candidate_id": "h24_unfiltered",
                "round_trip_cost_bps": 10,
                "total_return": 0.1,
                "profit_factor": 1.1,
                "random_entry_percentile": 0.6,
            },
        ),
        candidate_rows=tuple(candidate_rows),
        stitched_rows=(
            {
                "candidate_id": "h24_unfiltered",
                "round_trip_cost_bps": 10,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "equity": 100_000,
                "drawdown": 0,
                "rolling_sharpe_30d": 0,
            },
        ),
        holdout_rows=tuple(holdout_rows),
        regime_rows=(),
        cost_rows=tuple(
            {
                "candidate_id": row["candidate_id"],
                "round_trip_cost_bps": row["round_trip_cost_bps"],
                "stitched_oos_total_return": row["stitched_oos_total_return"],
            }
            for row in candidate_rows
        ),
        benchmark_rows=(),
        robustness_rows=(),
        bootstrap_rows=(
            {
                "candidate_id": "h24_unfiltered",
                "probability_final_return_below_zero": 0.4,
            },
        ),
        year_rows=({"candidate_id": "h24_unfiltered", "year": 2026, "net_return": -0.1},),
        concentration_rows=(
            {
                "scope": "stitched_oos",
                "removed_top_winners": 0,
                "total_return_after_removal": 0.1,
            },
            {
                "scope": "stitched_oos",
                "removed_top_winners": 5,
                "total_return_after_removal": -0.1,
            },
            {
                "scope": "stitched_oos",
                "removed_top_winners": 10,
                "total_return_after_removal": -0.2,
            },
        ),
        candidate_definitions=definitions,
        holdout_start="2026-01-01T00:00:00+00:00",
        holdout_execution_count=1,
    )


def test_walk_forward_artifacts_are_complete_and_reproducible(tmp_path: Path) -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    manifest = {"dataset_hash": "fixture", "normalized_rows": 100}
    first, second = tmp_path / "first", tmp_path / "second"
    for output in (first, second):
        write_walk_forward_artifacts(
            output,
            study=_study(),
            config=config,
            parameters=parameters,
            data_manifest=manifest,
            source_artifact_hashes={"episode": "e", "fixed_exit": "f"},
            strategy_file_hashes={},
            fixed_exit_context={
                "trade_count": 100,
                "concentration_rows": [
                    {"removed_top_winners": 0, "total_return_after_removal": 0.46},
                    {"removed_top_winners": 5, "total_return_after_removal": -0.1},
                    {"removed_top_winners": 10, "total_return_after_removal": -0.2},
                ],
                "profit_source_rows": [
                    {"period_type": "quarter", "period": "2024-Q1", "net_pnl": 100.0}
                ],
                "top_profit_periods": [
                    {"period_type": "quarter", "period": "2024-Q1", "net_pnl": 100.0}
                ],
            },
        )
    required = {
        "report.md",
        "summary.json",
        "walk_forward_windows.csv",
        "candidate_comparison.csv",
        "stitched_oos_equity.csv",
        "final_holdout.csv",
        "regime_filter_results.csv",
        "cost_stress_results.csv",
        "benchmark_results.csv",
        "parameter_robustness.csv",
        "bootstrap_results.csv",
        "profit_concentration.csv",
        "profit_by_period.csv",
        "artifact_manifest.json",
    }
    assert required.issubset({path.name for path in first.iterdir()})
    summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    assert summary["walk_forward_contract"]["final_holdout_execution_count"] == 1
    assert summary["production_strategy_changed"] is False
    assert summary["final_state"] in {
        "WALK_FORWARD_UNPROFITABLE",
        "WALK_FORWARD_COST_FRAGILE",
        "WALK_FORWARD_REGIME_FRAGILE",
        "WALK_FORWARD_TEMPORALLY_UNSTABLE",
        "WALK_FORWARD_OOS_WEAK",
        "WALK_FORWARD_PROMISING",
        "RESEARCH_CANDIDATE_V2_READY_FOR_HUMAN_REVIEW",
    }
    for name in required - {"summary.json", "report.md", "artifact_manifest.json"}:
        assert (first / name).read_bytes() == (second / name).read_bytes()
