"""Finalize V3 quality and database audit without rerunning research."""

from __future__ import annotations

import argparse
from pathlib import Path

from backtest.strategy_v3_artifacts import finalize_v3_quality
from scripts.finalize_strategy_v2_audit import _database_audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize Strategy V3 audit")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--targeted-tests", required=True)
    parser.add_argument("--full-pytest", required=True)
    parser.add_argument("--ruff", required=True)
    parser.add_argument("--mypy", required=True)
    parser.add_argument("--database", type=Path, default=Path("data/trading.db"))
    parser.add_argument("--orders-before", type=int, required=True)
    parser.add_argument("--fills-before", type=int, required=True)
    parser.add_argument("--budget-events-before", type=int, required=True)
    args = parser.parse_args()
    finalize_v3_quality(
        args.artifact,
        targeted_tests=args.targeted_tests,
        full_pytest=args.full_pytest,
        ruff=args.ruff,
        mypy=args.mypy,
        database_audit=_database_audit(
            args.database,
            orders_before=args.orders_before,
            fills_before=args.fills_before,
            budget_before=args.budget_events_before,
        ),
    )


if __name__ == "__main__":
    main()
