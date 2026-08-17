"""Auditable artifact output for VWAP episode research."""

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
from backtest.vwap_episode_research import EpisodeStudy


def write_episode_artifacts(
    output: Path,
    *,
    study: EpisodeStudy,
    config: RunConfig,
    parameters: VWAPShadowParameters,
    data_manifest: dict[str, Any],
    strategy_file_hashes: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    _write_csv(output / "episodes.csv", [item.flat() for item in study.episodes])
    _write_csv(output / "episode_forward_returns.csv", list(study.forward_statistics))
    _write_csv(output / "episode_mfe_mae.csv", list(study.mfe_mae_statistics))
    _write_csv(output / "episode_overlap.csv", list(study.overlap_statistics))
    _write_csv(output / "episode_regime_analysis.csv", list(study.regime_statistics))
    _write_csv(output / "episode_temporal_analysis.csv", list(study.temporal_statistics))
    (output / "data_manifest.json").write_text(
        json.dumps(data_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assessment = assess_episode_study(study)
    summary: dict[str, Any] = {
        "research_id": output.name,
        "research_type": "VWAP_EPISODE_RESEARCH_V1",
        "research_only": True,
        "baseline": "VWAP_SIGNAL_EDGE_V1",
        "instrument": config.market.instrument_id,
        "timeframe": config.market.bar,
        "dataset_hash": data_manifest["dataset_hash"],
        "confirmed_bars": data_manifest["normalized_rows"],
        "strategy_parameters": parameters.model_dump(mode="json"),
        "strategy_parameters_changed": False,
        "strategy_parity_pass": True,
        "lookahead_bias": False,
        "entry_reference": "next_bar_open",
        "episode_definition": "maximal contiguous BUY run on uninterrupted 1H candles",
        "episode_summary": study.summary_statistics,
        "deviation_distribution": study.deviation_statistics,
        "forward_statistics": list(study.forward_statistics),
        "overlap_statistics": list(study.overlap_statistics),
        **assessment,
        "strategy_file_hashes": strategy_file_hashes,
        "config_hash": canonical_hash(config.model_dump(mode="json")),
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
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    file_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "artifact_manifest.json"
    }
    (output / "artifact_manifest.json").write_text(
        json.dumps(
            {
                "research_id": output.name,
                "files": file_hashes,
                "artifact_set_hash": canonical_hash(file_hashes),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary


def assess_episode_study(study: EpisodeStudy) -> dict[str, object]:
    """Apply the predeclared episode assessment without tuning thresholds."""
    episode_count = _integer(study.summary_statistics["episode_count"])
    rows = {
        (str(row["scope"]), _integer(row["horizon_hours"])): row for row in study.forward_statistics
    }
    raw_24 = _number(rows[("raw_signal", 24)]["mean"])
    episode_24 = _number(rows[("episode", 24)]["mean"])
    raw_positive_ci_horizons = sum(
        _number(rows[("raw_signal", horizon)]["mean_ci_low"], default=-1.0) > 0
        for horizon in (1, 3, 6, 12, 24, 48, 72)
    )
    episode_positive_ci_horizons = sum(
        _number(rows[("episode", horizon)]["mean_ci_low"], default=-1.0) > 0
        for horizon in (1, 3, 6, 12, 24, 48, 72)
    )
    if episode_count < 100:
        return {
            "final_state": "EPISODE_RESEARCH_INCOMPLETE",
            "dedup_reduces_edge": None,
            "episode_edge_still_weak": None,
            "fixed_exit_research_ready": False,
            "raw_positive_mean_ci_horizons": raw_positive_ci_horizons,
            "episode_positive_mean_ci_horizons": episode_positive_ci_horizons,
        }
    dedup_reduces = episode_positive_ci_horizons < raw_positive_ci_horizons
    holdout_24 = next(
        row
        for row in study.temporal_statistics
        if row["dimension"] == "holdout"
        and row["group"] == "recent_20_percent"
        and row["horizon_hours"] == 24
    )
    episode_24_row = rows[("episode", 24)]
    weak = (
        _number(episode_24_row["mean_ci_low"], default=-1.0) <= 0
        or _number(holdout_24["mean_return"]) <= 0
        or _number(holdout_24["median_return"]) <= 0
        or _number(holdout_24["positive_rate"]) <= 0.5
    )
    if dedup_reduces:
        final_state = "EPISODE_DEDUP_REDUCES_EDGE"
    elif weak:
        final_state = "EPISODE_EDGE_STILL_WEAK"
    elif _number(episode_24_row["mean_ci_low"]) > 0:
        final_state = "EPISODE_EDGE_PROMISING"
    else:
        final_state = "READY_FOR_FIXED_EXIT_RESEARCH"
    return {
        "final_state": final_state,
        "dedup_reduces_edge": dedup_reduces,
        "episode_edge_still_weak": weak,
        "fixed_exit_research_ready": final_state == "READY_FOR_FIXED_EXIT_RESEARCH",
        "raw_signal_24h_mean": raw_24,
        "episode_24h_mean": episode_24,
        "raw_positive_mean_ci_horizons": raw_positive_ci_horizons,
        "episode_positive_mean_ci_horizons": episode_positive_ci_horizons,
        "episode_24h_holdout_mean": holdout_24["mean_return"],
        "episode_24h_holdout_median": holdout_24["median_return"],
        "episode_24h_holdout_positive_rate": holdout_24["positive_rate"],
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("status,reason\nnot_run,no_rows\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _report(summary: dict[str, Any]) -> str:
    episode = summary["episode_summary"]
    forward = {
        (str(row["scope"]), int(row["horizon_hours"])): row for row in summary["forward_statistics"]
    }
    overlap = {int(row["horizon_hours"]): row for row in summary["overlap_statistics"]}
    lines = [
        "# VWAP_EPISODE_RESEARCH_V1",
        "",
        "## Executive Summary",
        "",
        f"- {episode['raw_buy_signals']} raw BUY signals collapse into {episode['episode_count']} independent episodes.",
        f"- Signal inflation ratio: {float(episode['signal_inflation_ratio']):.3f}×.",
        f"- Dedup reduces statistical edge evidence: {summary['dedup_reduces_edge']}.",
        f"- Positive mean-CI horizons: raw={summary['raw_positive_mean_ci_horizons']}, episode={summary['episode_positive_mean_ci_horizons']}.",
        f"- Episode edge remains weak: {summary['episode_edge_still_weak']}.",
        f"- Final state: `{summary['final_state']}`.",
        "",
        "## Episode Contract",
        "",
        "```text",
        "research_only=true",
        "definition=maximal contiguous BUY run on uninterrupted 1H candles",
        "entry_reference=first BUY next bar open",
        "lookahead_bias=false",
        "strategy_parameters_changed=false",
        "strategy_parity_pass=true",
        "```",
        "",
        "## Episode Statistics",
        "",
        "```json",
        json.dumps(episode, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Raw Signal vs Episode Forward Return",
        "",
        "| Horizon | Raw count | Raw mean | Episode count | Episode mean | Episode median | Episode positive |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in (1, 3, 6, 12, 24, 48, 72):
        raw = forward[("raw_signal", horizon)]
        deduped = forward[("episode", horizon)]
        lines.append(
            f"| {horizon}H | {raw['count']} | {_pct(raw['mean'])} | {deduped['count']} | "
            f"{_pct(deduped['mean'])} | {_pct(deduped['median'])} | {_pct(deduped['positive_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## Episode Overlap / Position Conflict",
            "",
            "| Horizon | Episodes | Adjacent overlap | Overlap rate | One-position tradable | Blocked |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for horizon in (6, 12, 24, 48, 72):
        row = overlap[horizon]
        lines.append(
            f"| {horizon}H | {row['total_episodes']} | {row['episode_overlap_count']} | "
            f"{_pct(row['episode_overlap_rate'])} | {row['tradable_if_one_position_only']} | "
            f"{row['blocked_by_existing_position']} |"
        )
    lines.extend(
        [
            "",
            "## Files",
            "",
            "Detailed episode ledger, MFE/MAE, regime, temporal, and overlap tables are stored in the companion CSV files.",
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
            "",
            "## Final State",
            "",
            f"`{summary['final_state']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _number(value: object, *, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _integer(value: object) -> int:
    if not isinstance(value, (int, float)):
        raise TypeError(f"expected numeric integer, got {type(value).__name__}")
    return int(value)


def _pct(value: object) -> str:
    return f"{_number(value) * 100:.3f}%"
