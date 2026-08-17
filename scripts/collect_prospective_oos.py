"""CLI for bounded, public-only prospective OOS market-data collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.market.network import NetworkConfiguration
from backtest.prospective_artifacts import write_collector_artifacts
from backtest.prospective_collector import ProspectiveCollectorRunner, telemetry_dict
from backtest.prospective_oos import ProspectiveOOSStore

DEFAULT_DATA_ROOT = Path("data/prospective_oos")
DEFAULT_ARTIFACT_ROOT = Path("artifacts/prospective-oos")


def main() -> None:
    parser = argparse.ArgumentParser(description="Append-only OKX public prospective OOS collector")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--max-runtime-hours", type=float)
    parser.add_argument("--poll-seconds", type=int, default=600)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--database", type=Path, default=Path("data/trading.db"))
    args = parser.parse_args()
    runtime_seconds = 0.0 if args.once else float(args.max_runtime_hours) * 3600
    if not 0 <= runtime_seconds <= 8 * 3600:
        raise ValueError("max runtime must be between zero and eight hours")

    database_before = database_snapshot(args.database)
    protected = protected_hashes()
    network = NetworkConfiguration.from_environment()
    store = ProspectiveOOSStore(args.data_root)
    telemetry = ProspectiveCollectorRunner(store, network, poll_seconds=args.poll_seconds).run(
        max_runtime_seconds=runtime_seconds
    )
    manifest = store.write_root_manifest()
    integrity = store.integrity_report()
    output = args.artifact_root / (
        f"PROSPECTIVE_OOS_COLLECTOR_V1_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    summary = write_collector_artifacts(
        output,
        manifest=manifest,
        telemetry=telemetry_dict(telemetry),
        integrity=integrity,
        data_root=args.data_root,
        production_db_before=database_before,
        protected_hashes=protected,
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "final_state": summary["final_state"],
                "prospective_rows": manifest["total_rows"],
                "dataset_root_hash": manifest["dataset_root_hash"],
            },
            ensure_ascii=False,
        )
    )


def database_snapshot(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        result: dict[str, object] = {
            "database_version": int(
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            ),
            "integrity_check": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "active_run_locks": int(
                connection.execute(
                    """SELECT COUNT(*) FROM continuous_run_locks
                    WHERE released_at IS NULL
                    AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"""
                ).fetchone()[0]
            ),
            "active_runs": int(
                connection.execute(
                    """SELECT COUNT(*) FROM continuous_demo_runs
                    WHERE status IN ('starting','running','stopping','recovering')"""
                ).fetchone()[0]
            ),
            "orders": int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
            "fills": int(connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]),
            "budget_events": int(
                connection.execute("SELECT COUNT(*) FROM bounded_submission_events").fetchone()[0]
            ),
        }
    finally:
        connection.close()
    result.update(
        {
            "file_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
        }
    )
    return result


def protected_hashes() -> dict[str, str]:
    paths = (
        Path("configs/btc_vwap_shadow.yaml"),
        Path("app/strategies/vwap_shadow.py"),
        Path("app/execution/demo_broker.py"),
        Path("app/risk/risk_manager.py"),
    )
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


if __name__ == "__main__":
    main()
