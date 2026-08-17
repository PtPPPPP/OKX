"""Run the frozen, read-only VWAP signal-edge research pipeline."""

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
from backtest.vwap_signal_edge import parameter_sensitivity, run_signal_edge_study
from backtest.vwap_signal_edge_artifacts import write_signal_edge_artifacts
from backtest.vwap_signal_edge_data import load_normalized_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Formal VWAP BUY signal edge research")
    parser.add_argument("--config", type=Path, default=Path("configs/btc_vwap_shadow.yaml"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/backtests"))
    arguments = parser.parse_args()
    config = load_run_config(arguments.config, environ={})
    if config.strategy.name != "vwap_shadow":
        raise ValueError("signal edge research requires the formal vwap_shadow configuration")
    manifest_path = arguments.cache / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require_data_gate(manifest)
    candles = load_normalized_candles(
        arguments.cache / "normalized" / "candles.csv", bar=config.market.bar
    )
    instrument_path = config.data.instrument_snapshot
    if instrument_path is None:
        raise ValueError("formal VWAP configuration must freeze an instrument snapshot")
    instrument = InstrumentSnapshotStore.load(instrument_path).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    study = run_signal_edge_study(candles, instrument, parameters)
    sensitivity = parameter_sensitivity(candles, instrument)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = arguments.output_root / f"VWAP_SIGNAL_EDGE_V1_{timestamp}"
    freeze_paths = (
        arguments.config,
        Path("app/strategies/vwap_shadow.py"),
        Path("backtest/vwap_shadow_research.py"),
    )
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in freeze_paths}
    summary = write_signal_edge_artifacts(
        output,
        study=study,
        candles=candles,
        config=config,
        parameters=parameters,
        data_manifest=manifest,
        parameter_rows=sensitivity,
        strategy_file_hashes=hashes,
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "confirmed_bars": summary["confirmed_bars"],
                "raw_signal_count": summary["raw_signal_count"],
                "signal_episode_count": summary["signal_episode_count"],
                "exit_readiness": summary["exit_readiness"],
                "strategy_assessment": summary["strategy_assessment"],
            },
            ensure_ascii=False,
        )
    )


def _require_data_gate(manifest: dict[str, Any]) -> None:
    if manifest.get("status") != "complete":
        raise ValueError("research data manifest is not complete")
    for name in ("duplicate_count", "missing_count", "invalid_ohlc_count", "out_of_order_count"):
        if int(manifest.get(name, -1)) != 0:
            raise ValueError(f"research data quality gate failed: {name}")
    if manifest.get("confirmed_candle_only") is not True:
        raise ValueError("research data is not confirmed-candle-only")


if __name__ == "__main__":
    main()
