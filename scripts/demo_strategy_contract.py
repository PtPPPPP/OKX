"""Compare batch signal replay, confirmed read-only input and checkpoint restoration offline."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.config.run_config import RunConfig
from app.domain.market import Candle
from app.domain.position import PortfolioSnapshot
from app.reproducibility import InstrumentSnapshotStore, canonical_hash
from app.services.strategy_shadow import ConfirmedStrategyShadow
from app.strategies.v3 import V3Strategy
from backtest.research_governance import PROJECT_ROOT, file_hash, read_json, write_json_once
from backtest.strategy_v3_candidates import load_v3_specs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data", type=Path, default=PROJECT_ROOT / "tests/fixtures/vwap/btc_usdt_1h_600.csv"
    )
    parser.add_argument("--candidate", default="relative_volume_breakout")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec_path = PROJECT_ROOT / "configs/research/strategy_v3_candidate_specs.json"
    specs = load_v3_specs(spec_path)
    cutoff = datetime.fromisoformat(read_json(spec_path)["research_cutoff"])
    variant = next(
        (item for item in specs if item.primary and item.candidate_id == args.candidate), None
    )
    if variant is None:
        raise ValueError(
            "demo requires one of the existing PRIMARY variants; parameters are not overridden"
        )
    # Preserve input order. Contract validation, rather than sorting/deduplication, handles bad input.
    with args.data.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    bars = [
        Candle(
            timestamp=datetime.fromisoformat(row["timestamp"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]),
            confirmed=row["confirmed"] == "1",
        )
        for row in rows
    ]
    if len(bars) < 302 or any(bar.timestamp >= cutoff for bar in bars):
        raise ValueError("demo needs at least 302 development/fixture candles, never frozen OOS")
    configuration = RunConfig.model_validate(
        {
            "mode": "backtest",
            "market": {"instrument_id": "BTC-USDT", "bar": "1h"},
            "strategy": {
                "name": variant.candidate_id,
                "implementation_version": V3Strategy.implementation_version,
                "parameters": variant.parameters,
            },
            "data": {"source": "csv", "path": args.data},
        }
    )
    instrument = InstrumentSnapshotStore.load(
        PROJECT_ROOT / "configs/snapshots/BTC-USDT.json"
    ).instrument
    initial = PortfolioSnapshot({"USDT": Decimal("10000")}, {}, {}, trusted_for_trading=False)
    batch = ConfirmedStrategyShadow(configuration, instrument, initial, run_id="batch")
    stepped = ConfirmedStrategyShadow(configuration, instrument, initial, run_id="confirmed-input")
    if batch.replay(bars[:300], warmup=True):
        raise ValueError("warmup produced signals")
    for candle in bars[:300]:
        if stepped.on_candle(candle, warmup=True):
            raise ValueError("warmup produced signals")
    expected = batch.replay(bars[300:])
    midpoint = 300 + (len(bars) - 300) // 2
    actual = [signal for candle in bars[300:midpoint] for signal in stepped.on_candle(candle)]
    checkpoint = stepped.checkpoint_state()
    restored = ConfirmedStrategyShadow(
        configuration, instrument, initial, run_id="restored", resume_state=checkpoint
    )
    actual.extend(signal for candle in bars[midpoint:] for signal in stepped.on_candle(candle))
    resumed = restored.replay(bars[midpoint:])
    if (
        expected != actual
        or resumed != expected[midpoint - 300 :]
        or batch.checkpoint_state() != stepped.checkpoint_state()
        or restored.checkpoint_state() != batch.checkpoint_state()
    ):
        raise ValueError("strategy contract mismatch; no success report published")
    args.output.mkdir(parents=True, exist_ok=False)
    batch_rows = [asdict(item) for item in expected]
    step_rows = [asdict(item) for item in actual]
    write_json_once(
        args.output / "batch_signals.json", {"identity": batch.identity, "signals": batch_rows}
    )
    write_json_once(
        args.output / "step_signals.json", {"identity": stepped.identity, "signals": step_rows}
    )
    write_json_once(args.output / "checkpoint.json", checkpoint)
    report = {
        "classification": "ENGINEERING_DEVELOPMENT_FIXTURE_NOT_OOS_EVIDENCE",
        "strategy_identity": batch.identity,
        "variant_id": variant.variant_id,
        "input_sha256": file_hash(args.data),
        "input_bars": len(bars),
        "warmup_bars": 300,
        "signal_count": len(expected),
        "actionable_signals": sum(item.action.value != "hold" for item in expected),
        "signal_fingerprint": canonical_hash(batch_rows),
        "batch_equals_step": True,
        "resume_equals_continuous": True,
        "resumed_state_equals_continuous": True,
        "orders_or_fills_inferred": False,
        "trading_permission": False,
    }
    write_json_once(args.output / "report.json", report)
    import json

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
