"""Run the frozen, read-only VWAP episode research pipeline."""

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
from backtest.vwap_episode_artifacts import write_episode_artifacts
from backtest.vwap_episode_research import run_episode_study
from backtest.vwap_signal_edge_data import load_normalized_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only VWAP BUY episode study")
    parser.add_argument("--config", type=Path, default=Path("configs/btc_vwap_shadow.yaml"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/backtests"))
    arguments = parser.parse_args()
    config = load_run_config(arguments.config, environ={})
    if config.strategy.name != "vwap_shadow":
        raise ValueError("episode research requires formal vwap_shadow configuration")
    manifest = json.loads((arguments.cache / "data_manifest.json").read_text(encoding="utf-8"))
    _require_data_gate(manifest)
    candles = load_normalized_candles(
        arguments.cache / "normalized" / "candles.csv", bar=config.market.bar
    )
    snapshot_path = config.data.instrument_snapshot
    if snapshot_path is None:
        raise ValueError("formal VWAP configuration must freeze an instrument snapshot")
    instrument = InstrumentSnapshotStore.load(snapshot_path).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    study = run_episode_study(candles, instrument, parameters)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = arguments.output_root / f"VWAP_EPISODE_V1_{timestamp}"
    freeze_paths = (
        arguments.config,
        Path("app/strategies/vwap_shadow.py"),
        Path("backtest/vwap_shadow_research.py"),
    )
    summary = write_episode_artifacts(
        output,
        study=study,
        config=config,
        parameters=parameters,
        data_manifest=manifest,
        strategy_file_hashes={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in freeze_paths
        },
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "raw_buy_signals": summary["episode_summary"]["raw_buy_signals"],
                "episode_count": summary["episode_summary"]["episode_count"],
                "final_state": summary["final_state"],
            },
            ensure_ascii=False,
        )
    )


def _require_data_gate(manifest: dict[str, Any]) -> None:
    if manifest.get("status") != "complete":
        raise ValueError("episode research data manifest is not complete")
    for name in ("duplicate_count", "missing_count", "invalid_ohlc_count", "out_of_order_count"):
        if int(manifest.get(name, -1)) != 0:
            raise ValueError(f"episode research data quality gate failed: {name}")
    if manifest.get("confirmed_candle_only") is not True:
        raise ValueError("episode research data is not confirmed-candle-only")


if __name__ == "__main__":
    main()
