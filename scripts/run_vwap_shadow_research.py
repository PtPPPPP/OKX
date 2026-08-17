from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from app.config.run_config import load_run_config
from app.domain.market import Instrument
from app.market.historical_data import load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_shadow_research import write_research_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only VWAP Shadow signal baseline")
    parser.add_argument("--config", type=Path, default=Path("configs/btc_vwap_shadow.yaml"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    config = load_run_config(arguments.config, environ={})
    if config.strategy.name != "vwap_shadow":
        raise ValueError("research baseline only accepts the production vwap_shadow strategy")

    def cache_miss(_instrument_id: str) -> Instrument:
        raise RuntimeError("instrument snapshot is required for an offline research run")

    instrument = (
        InstrumentSnapshotStore()
        .resolve(
            config.market.instrument_id,
            configured_path=config.data.instrument_snapshot,
            fetch=cache_miss,
        )
        .instrument
    )
    candles = load_candles_csv(arguments.data, bar=config.market.bar)
    output = arguments.output / f"VWAP_BASELINE_V1_{datetime.now().strftime('%Y%m%dT%H%M%SZ')}"
    write_research_artifacts(
        output,
        candles=candles,
        instrument=instrument,
        parameters=VWAPShadowParameters.model_validate(config.strategy.parameters),
        data_source=arguments.data,
    )
    print(output)


if __name__ == "__main__":
    main()
