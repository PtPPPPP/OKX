"""Read explicit local files and run a finite portfolio; never load .env or REST."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.market.historical_data import load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from backtest.portfolio import PortfolioBacktestEngine, PortfolioConfig, write_immutable_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--instrument", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory; never overwrite")
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    config = PortfolioConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    instrument = InstrumentSnapshotStore.load(args.instrument).instrument
    candles = load_candles_csv(args.candles, bar=config.market.bar, strict=True)
    runner = (
        PortfolioBacktestEngine.restore(args.resume, config, instrument)
        if args.resume
        else PortfolioBacktestEngine(config, instrument, run_id="offline-portfolio")
    )
    # Resuming accepts only the exact complete data prefix followed by new bars.
    if runner.candles and candles[: len(runner.candles)] != runner.candles:
        raise ValueError("resume data prefix differs from checkpoint history")
    args.output.mkdir(parents=True, exist_ok=False)
    for candle in candles[len(runner.candles) :]:
        runner.advance(candle)
    runner.save_checkpoint(args.output / "checkpoint.json")
    report = runner.finish()
    write_immutable_json(args.output / "report.json", report)
    print(json.dumps({"output": str(args.output), "account": report["account"]}, default=str))


if __name__ == "__main__":
    main()
