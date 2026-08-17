"""Read recovery-relevant SQLite records without changing the database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def rows(connection: sqlite3.Connection, statement: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(statement)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        output = {
            "tables": [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ],
            "schemas": {
                table: rows(connection, f"PRAGMA table_info({table})")
                for table in (
                    "continuous_demo_runs",
                    "continuous_run_locks",
                    "continuous_run_recoveries",
                    "orders",
                    "fills",
                )
            },
            "runs": rows(connection, "SELECT * FROM continuous_demo_runs"),
            "locks": rows(connection, "SELECT * FROM continuous_run_locks"),
            "recoveries": rows(connection, "SELECT * FROM continuous_run_recoveries"),
            "orders": rows(connection, "SELECT * FROM orders"),
            "fills": rows(connection, "SELECT * FROM fills"),
        }
    finally:
        connection.close()
    print(json.dumps(output, default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
