"""Finalize collector quality and safety evidence without rerunning collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from backtest.prospective_artifacts import finalize_collector_artifacts
from scripts.collect_prospective_oos import database_snapshot, protected_hashes


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize prospective OOS collector audit")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--targeted-tests", required=True)
    parser.add_argument("--full-pytest", required=True)
    parser.add_argument("--ruff", required=True)
    parser.add_argument("--mypy", required=True)
    parser.add_argument("--database", type=Path, default=Path("data/trading.db"))
    args = parser.parse_args()
    finalize_collector_artifacts(
        args.artifact,
        targeted_tests=args.targeted_tests,
        full_pytest=args.full_pytest,
        ruff=args.ruff,
        mypy=args.mypy,
        production_db_after=database_snapshot(args.database),
        protected_hashes_after=protected_hashes(),
    )


if __name__ == "__main__":
    main()
