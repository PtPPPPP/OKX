"""Run the frozen, offline VWAP fixed-exit research pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.run_config import load_run_config
from app.reproducibility import InstrumentSnapshotStore
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_episode_research import run_episode_study
from backtest.vwap_fixed_exit_artifacts import write_fixed_exit_artifacts
from backtest.vwap_fixed_exit_research import run_fixed_exit_study
from backtest.vwap_signal_edge_data import load_normalized_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline VWAP fixed-exit portfolio research")
    parser.add_argument("--config", type=Path, default=Path("configs/btc_vwap_shadow.yaml"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--episode-artifact", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/backtests"))
    args = parser.parse_args()
    config = load_run_config(args.config, environ={})
    if config.strategy.name != "vwap_shadow":
        raise ValueError("fixed-exit research requires formal vwap_shadow configuration")
    manifest = json.loads((args.cache / "data_manifest.json").read_text(encoding="utf-8"))
    _require_data_gate(manifest)
    episode_manifest = json.loads(
        (args.episode_artifact / "artifact_manifest.json").read_text(encoding="utf-8")
    )
    if not str(episode_manifest.get("research_id", "")).startswith("VWAP_EPISODE_V1_"):
        raise ValueError("fixed-exit research requires a VWAP_EPISODE_V1 artifact")
    episode_summary = json.loads(
        (args.episode_artifact / "summary.json").read_text(encoding="utf-8")
    )
    if episode_summary.get("dataset_hash") != manifest.get("dataset_hash"):
        raise ValueError("episode artifact and candle dataset hashes differ")

    candles = load_normalized_candles(
        args.cache / "normalized" / "candles.csv", bar=config.market.bar
    )
    snapshot = config.data.instrument_snapshot
    if snapshot is None:
        raise ValueError("formal VWAP configuration must freeze an instrument snapshot")
    instrument = InstrumentSnapshotStore.load(snapshot).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    episode_study = run_episode_study(candles, instrument, parameters)
    study = run_fixed_exit_study(candles, episode_study.episodes)
    output = args.output_root / f"VWAP_FIXED_EXIT_V1_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    frozen = (
        args.config,
        Path("app/strategies/vwap_shadow.py"),
        Path("backtest/vwap_shadow_research.py"),
    )
    summary = write_fixed_exit_artifacts(
        output,
        study=study,
        config=config,
        parameters=parameters,
        data_manifest=manifest,
        strategy_file_hashes={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen
        },
        episode_artifact_hash=str(episode_manifest["artifact_set_hash"]),
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "historical_best_horizon": summary["historical_best_horizon"],
                "oos_best_horizon": summary["oos_best_horizon"],
                "final_state": summary["final_state"],
            },
            ensure_ascii=False,
        )
    )


def _require_data_gate(manifest: dict[str, Any]) -> None:
    if manifest.get("status") != "complete" or manifest.get("confirmed_candle_only") is not True:
        raise ValueError("fixed-exit research data gate failed")
    for field in ("duplicate_count", "missing_count", "invalid_ohlc_count", "out_of_order_count"):
        if int(manifest.get(field, -1)) != 0:
            raise ValueError(f"fixed-exit research data quality gate failed: {field}")


if __name__ == "__main__":
    main()
