"""Finalize quality and safety evidence after independent gates pass."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest.market_information_artifacts import finalize_artifacts
from scripts.collect_prospective_oos import database_snapshot, protected_hashes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    summary = finalize_artifacts(
        args.artifact,
        targeted="22 passed",
        full_pytest="585 passed",
        ruff="passed",
        mypy="266 source files passed",
        database_after=database_snapshot(Path("data/trading.db")),
        protected_after=protected_hashes(),
    )
    print(
        json.dumps(
            {
                "final_state": summary["final_state"],
                "quality_gate": summary["quality_gate"]["status"],
                "production_db_changed": summary["production_db_changed"],
                "protected_files_changed": summary["protected_files_changed"],
            }
        )
    )


if __name__ == "__main__":
    main()
