from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backtest.engine import BacktestResult

TRADE_FIELDS = [
    "run_id",
    "strategy_name",
    "instrument_id",
    "timestamp",
    "side",
    "quantity",
    "reference_price",
    "fill_price",
    "notional",
    "fee",
    "slippage_cost",
    "signal_id",
    "client_order_id",
]
EQUITY_FIELDS = [
    "run_id",
    "strategy_name",
    "instrument_id",
    "bar",
    "timestamp",
    "equity",
    "quote_balance",
    "base_quantity",
    "mark_price",
]


def write_backtest_report(result: BacktestResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        output_dir / "backtest_trades.csv",
        [asdict(fill) for fill in result.fills],
        TRADE_FIELDS,
    )
    _write_csv(
        output_dir / "equity_curve.csv",
        [asdict(point) for point in result.equity_curve],
        EQUITY_FIELDS,
    )
    with (output_dir / "backtest_summary.json").open("w", encoding="utf-8") as file:
        json.dump(result.summary, file, ensure_ascii=False, indent=2, default=str)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
