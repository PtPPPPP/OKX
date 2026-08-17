"""Run strict offline VWAP walk-forward research from frozen artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.run_config import load_run_config
from app.reproducibility import InstrumentSnapshotStore, canonical_hash
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_episode_research import run_episode_study
from backtest.vwap_signal_edge_data import load_normalized_candles
from backtest.vwap_walk_forward_artifacts import write_walk_forward_artifacts
from backtest.vwap_walk_forward_research import analyze_fixed_exit_trades, run_walk_forward_study


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline VWAP walk-forward research")
    parser.add_argument("--config", type=Path, default=Path("configs/btc_vwap_shadow.yaml"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--episode-artifact", type=Path, required=True)
    parser.add_argument("--fixed-exit-artifact", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/backtests"))
    args = parser.parse_args()
    config = load_run_config(args.config, environ={})
    if config.strategy.name != "vwap_shadow":
        raise ValueError("walk-forward research requires formal vwap_shadow configuration")
    manifest = json.loads((args.cache / "data_manifest.json").read_text(encoding="utf-8"))
    _require_data_gate(manifest)
    episode_hash = _require_artifact(
        args.episode_artifact, "VWAP_EPISODE_V1_", manifest["dataset_hash"]
    )
    fixed_hash = _require_artifact(
        args.fixed_exit_artifact, "VWAP_FIXED_EXIT_V1_", manifest["dataset_hash"]
    )
    fixed_summary = json.loads(
        (args.fixed_exit_artifact / "summary.json").read_text(encoding="utf-8")
    )
    if int(fixed_summary["historical_best_horizon"]) != 24:
        raise ValueError("walk-forward V1 requires the frozen 24H primary candidate")
    with (args.fixed_exit_artifact / "trades_24h.csv").open(encoding="utf-8", newline="") as file:
        fixed_exit_context = analyze_fixed_exit_trades(csv.DictReader(file))
    candles = load_normalized_candles(
        args.cache / "normalized" / "candles.csv", bar=config.market.bar
    )
    snapshot = config.data.instrument_snapshot
    if snapshot is None:
        raise ValueError("formal VWAP configuration must freeze an instrument snapshot")
    instrument = InstrumentSnapshotStore.load(snapshot).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    episodes = run_episode_study(candles, instrument, parameters).episodes
    study = run_walk_forward_study(candles, episodes)
    output = (
        args.output_root / f"VWAP_WALK_FORWARD_V1_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    frozen = (
        args.config,
        Path("app/strategies/vwap_shadow.py"),
        Path("backtest/vwap_shadow_research.py"),
    )
    summary = write_walk_forward_artifacts(
        output,
        study=study,
        config=config,
        parameters=parameters,
        data_manifest=manifest,
        source_artifact_hashes={"episode": episode_hash, "fixed_exit": fixed_hash},
        strategy_file_hashes={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen
        },
        fixed_exit_context=fixed_exit_context,
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "test_windows": len(study.windows),
                "holdout_execution_count": study.holdout_execution_count,
                "final_state": summary["final_state"],
            },
            ensure_ascii=False,
        )
    )


def _require_artifact(path: Path, prefix: str, dataset_hash: str) -> str:
    if (path / "INVALIDATED.json").exists():
        raise ValueError(f"source artifact is invalidated: {path}")
    artifact = json.loads((path / "artifact_manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    if not str(artifact.get("research_id", "")).startswith(prefix):
        raise ValueError(f"source artifact type mismatch: {path}")
    if summary.get("dataset_hash") != dataset_hash:
        raise ValueError(f"source artifact dataset mismatch: {path}")
    declared_files = artifact.get("files")
    if not isinstance(declared_files, dict):
        raise ValueError(f"source artifact manifest is malformed: {path}")
    actual_files = {
        str(name): hashlib.sha256((path / str(name)).read_bytes()).hexdigest()
        for name in declared_files
    }
    if actual_files != declared_files or canonical_hash(actual_files) != artifact.get(
        "artifact_set_hash"
    ):
        raise ValueError(f"source artifact hash verification failed: {path}")
    return str(artifact["artifact_set_hash"])


def _require_data_gate(manifest: dict[str, Any]) -> None:
    if manifest.get("status") != "complete" or manifest.get("confirmed_candle_only") is not True:
        raise ValueError("walk-forward data gate failed")
    for field in (
        "duplicate_count",
        "missing_count",
        "invalid_ohlc_count",
        "out_of_order_count",
    ):
        if int(manifest.get(field, -1)) != 0:
            raise ValueError(f"walk-forward data quality gate failed: {field}")


if __name__ == "__main__":
    main()
