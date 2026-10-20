"""Deterministic two-instance offline demonstration; no network or account setup."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.domain.market import Candle
from app.reproducibility import InstrumentSnapshotStore, canonical_hash
from backtest.portfolio import PortfolioBacktestEngine, PortfolioConfig, write_immutable_json


def demo_config() -> PortfolioConfig:
    return PortfolioConfig.model_validate_json(
        (Path(__file__).resolve().parents[1] / "configs/portfolio.offline.example.json").read_text(
            encoding="utf-8"
        )
    )


def demo_candles() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            start + timedelta(minutes=5 * index),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0.5"),
            True,
        )
        for index in range(5)
    ]


def demonstrate(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    instrument = InstrumentSnapshotStore.load(
        Path(__file__).resolve().parents[1] / "configs/snapshots/BTC-USDT.json"
    ).instrument
    config, candles = demo_config(), demo_candles()
    runner = PortfolioBacktestEngine(config, instrument, run_id="offline-portfolio-example")
    for candle in candles[:2]:
        runner.advance(candle)
    partial = runner.report()
    runner.save_checkpoint(output / "checkpoint.json")
    restored = PortfolioBacktestEngine.restore(output / "checkpoint.json", config, instrument)
    for candle in candles[2:]:
        runner.advance(candle)
        restored.advance(candle)
    uninterrupted, resumed = runner.finish(), restored.finish()
    if canonical_hash(uninterrupted) != canonical_hash(resumed):
        raise ValueError("resumed portfolio differs from uninterrupted replay")
    result = {
        "fixture": "synthetic engineering data; not research/OOS",
        "network_access": False,
        "checkpoint_with_partial_fills": partial["account"],
        "checkpoint_strategies": partial["strategies"],
        "final_account": resumed["account"],
        "final_strategies": resumed["strategies"],
        "exact_resume_match": True,
        "account_conservation": True,
        "report_hash": canonical_hash(resumed),
    }
    write_immutable_json(output / "config.json", config.model_dump(mode="json"))
    write_immutable_json(output / "report.json", resumed)
    write_immutable_json(output / "demonstration.json", result)
    return result


def main() -> None:
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    print(json.dumps(demonstrate(args.output), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
