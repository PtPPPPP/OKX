"""Run frozen Strategy Research V3 on the pre-cutoff local dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from backtest.strategy_v3_artifacts import write_strategy_v3_artifacts
from backtest.strategy_v3_candidates import load_v3_specs
from backtest.strategy_v3_features import (
    aggregate_completed,
    validate_research_partition,
    validate_volumes,
)
from backtest.strategy_v3_research import run_strategy_v3_study
from backtest.vwap_signal_edge_data import load_normalized_candles
from scripts.run_strategy_v2_research import _require_artifact, _require_data_gate


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Strategy Research V3")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--specs", type=Path, default=Path("configs/research/strategy_v3_candidate_specs.json")
    )
    parser.add_argument("--vwap-episode-artifact", type=Path, required=True)
    parser.add_argument("--vwap-walk-forward-artifact", type=Path, required=True)
    parser.add_argument("--strategy-v2-artifact", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/backtests"))
    args = parser.parse_args()
    manifest = json.loads((args.cache / "data_manifest.json").read_text(encoding="utf-8"))
    _require_data_gate(manifest)
    spec_payload = json.loads(args.specs.read_text(encoding="utf-8"))
    sources = {
        "VWAP_EPISODE_V1": _require_artifact(
            args.vwap_episode_artifact, "VWAP_EPISODE_V1_", manifest["dataset_hash"]
        ),
        "VWAP_WALK_FORWARD_V1": _require_artifact(
            args.vwap_walk_forward_artifact, "VWAP_WALK_FORWARD_V1_", manifest["dataset_hash"]
        ),
        "STRATEGY_RESEARCH_V2": _require_artifact(
            args.strategy_v2_artifact, "STRATEGY_RESEARCH_V2_", manifest["dataset_hash"]
        ),
    }
    v2_summary = json.loads(
        (args.strategy_v2_artifact / "summary.json").read_text(encoding="utf-8")
    )
    if v2_summary["final_state"] != "NO_STRATEGY_CANDIDATE_FOUND":
        raise ValueError("V3 requires the archived V2 rejection baseline")
    episode_summary = json.loads(
        (args.vwap_episode_artifact / "summary.json").read_text(encoding="utf-8")
    )
    vwap_means = {
        int(row["horizon_hours"]): float(row["mean"])
        for row in episode_summary["forward_statistics"]
        if row["scope"] == "episode"
    }
    v2_rows = list(v2_summary["candidate_scoreboard"])
    variant_rows = _read_csv(args.strategy_v2_artifact / "variant_forward_results.csv")
    v2_means: dict[str, dict[int, float]] = {}
    for candidate in ("price_breakout", "momentum_pullback", "confirmed_mean_reversion"):
        primary = next(
            str(row["primary_variant_id"]) for row in v2_rows if row["candidate_id"] == candidate
        )
        v2_means[candidate] = {
            int(row["horizon_hours"]): float(row["mean"])
            for row in variant_rows
            if row["variant_id"] == primary and row["scope"] == "development"
        }
    candles = load_normalized_candles(args.cache / "normalized" / "candles.csv", bar="1h")
    validate_research_partition(
        candles,
        research_cutoff=datetime.fromisoformat(spec_payload["research_cutoff"]),
        prospective_start=datetime.fromisoformat(spec_payload["prospective_oos_start"]),
    )
    bars4h = aggregate_completed(candles, hours=4)
    bars1d = aggregate_completed(candles, hours=24)
    feature_manifest = {
        **validate_volumes(candles),
        "aggregated_4h_bars": len(bars4h),
        "aggregated_1d_bars": len(bars1d),
        "incomplete_4h_excluded": True,
        "incomplete_1d_excluded": True,
        "higher_timeframe_lookahead_bias": False,
        "volume_documentation": "OKX API v5: SPOT vol is base currency; volCcy/volCcyQuote are quote currency",
        "volume_documentation_url": "https://www.okx.com/docs-v5/en/#rest-api-market-data-get-candlesticks",
    }
    variants = load_v3_specs(args.specs)
    study = run_strategy_v3_study(
        candles, bars4h, variants, vwap_forward_means=vwap_means, v2_forward_means=v2_means
    )
    output = (
        args.output_root / f"STRATEGY_RESEARCH_V3_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    frozen = (
        args.specs,
        Path("configs/btc_vwap_shadow.yaml"),
        Path("app/strategies/vwap_shadow.py"),
    )
    summary = write_strategy_v3_artifacts(
        output,
        study=study,
        spec_payload=spec_payload,
        data_manifest=manifest,
        feature_manifest=feature_manifest,
        source_artifact_hashes=sources,
        frozen_file_hashes={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen
        },
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "candidate_count": len(study.results),
                "variant_count": study.variant_count,
                "survivor": study.surviving_candidate,
                "final_state": summary["final_state"],
            },
            ensure_ascii=False,
        )
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


if __name__ == "__main__":
    main()
