"""Record completed external quality gates without rerunning Strategy V2 research."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from backtest.strategy_v2_artifacts import finalize_quality_gate


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize Strategy V2 quality audit")
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
    finalize_quality_gate(
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


def _database_audit(
    path: Path, *, orders_before: int, fills_before: int, budget_before: int
) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        version = int(
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        )
        active_locks = int(
            connection.execute(
                """SELECT COUNT(*) FROM continuous_run_locks
                WHERE released_at IS NULL
                AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"""
            ).fetchone()[0]
        )
        active_runs = int(
            connection.execute(
                """SELECT COUNT(*) FROM continuous_demo_runs
                WHERE status IN ('starting','running','stopping','recovering')"""
            ).fetchone()[0]
        )
        orders_after = int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
        fills_after = int(connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
        budget_after = int(
            connection.execute("SELECT COUNT(*) FROM bounded_submission_events").fetchone()[0]
        )
    finally:
        connection.close()
    return {
        "database_version": version,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "active_run_locks": active_locks,
        "active_runs": active_runs,
        "orders_before": orders_before,
        "orders_after": orders_after,
        "orders_created_this_task": orders_after - orders_before,
        "fills_before": fills_before,
        "fills_after": fills_after,
        "fills_created_this_task": fills_after - fills_before,
        "budget_events_before": budget_before,
        "budget_events_after": budget_after,
        "submission_budget_events_created": budget_after - budget_before,
    }


if __name__ == "__main__":
    main()
