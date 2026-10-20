from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.execution.backtest_broker import ClosedTrade
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
    "exit_reason",
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
    scoped = any(fill.strategy_instance_id for fill in result.fills)
    trade_fields = [*TRADE_FIELDS, "strategy_instance_id"] if scoped else TRADE_FIELDS
    closed_fields = [
        name
        for name in ClosedTrade.__dataclass_fields__
        if scoped or name != "strategy_instance_id"
    ]
    _write_csv(
        output_dir / "backtest_trades.csv",
        [
            {name: value for name, value in asdict(fill).items() if name in trade_fields}
            for fill in result.fills
        ],
        trade_fields,
    )
    _write_csv(
        output_dir / "equity_curve.csv",
        [asdict(point) for point in result.equity_curve],
        EQUITY_FIELDS,
    )
    _write_csv(
        output_dir / "closed_trades.csv",
        [
            {name: value for name, value in asdict(trade).items() if name in closed_fields}
            for trade in result.closed_trades
        ],
        closed_fields,
    )
    with (output_dir / "backtest_summary.json").open("w", encoding="utf-8") as file:
        json.dump(result.summary, file, ensure_ascii=False, indent=2, default=str)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
