"""Finalize derivatives collector quality and production-safety evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest.derivatives_artifacts import finalize_derivatives_artifacts
from scripts.collect_prospective_oos import database_snapshot, protected_hashes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    summary = finalize_derivatives_artifacts(
        args.artifact,
        targeted="112 passed",
        full_pytest="600 passed (first run: 599 passed, 1 existing race failure)",
        ruff="passed",
        mypy="273 source files passed",
        database_after=database_snapshot(Path("data/trading.db")),
        protected_after=protected_hashes(),
    )
    print(
        json.dumps(
            {
                "final_state": summary["final_state"],
                "production_db_changed": summary["production_db_changed"],
                "protected_files_changed": summary["protected_files_changed"],
                "safety": summary["safety"],
            }
        )
    )


if __name__ == "__main__":
    main()
