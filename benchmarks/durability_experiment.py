"""Replay-scoped durability experiment: WAL+FULL vs production WAL+NORMAL.

Answers two questions with data for the Phase 2B4 replay durability contract:

1. How much does `PRAGMA synchronous=NORMAL` speed up the canonical
   shadow-replay workload, and how do the cost components shift?
2. What does each mode actually guarantee under process termination
   (before/after commit)? OS crash and power loss are explicitly NOT_TESTED.

Production replay owns NORMAL. The benchmark-only FULL control overrides only
``Database.open_reconstructible_replay_connection`` after the same factory has
configured it; default and critical ``Database.connect`` calls stay FULL.

Usage:
    uv run python -m benchmarks.durability_experiment [--output PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.config.run_config import load_run_config
from app.market.historical_data import save_candles_csv
from app.market.synthetic_candles import SyntheticCandleRequest, generate_synthetic_candles
from app.storage.database import Database
from benchmarks.persistence_metrics import PersistenceMetrics, _CountingConnection

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / "configs" / "btc_vwap_shadow.yaml"
_SYNTHETIC_SEED = 20260731

FULL = 2
NORMAL = 1
_MODE_NAMES = {FULL: "FULL", NORMAL: "NORMAL"}


@contextmanager
def experiment_sqlite(
    metrics: PersistenceMetrics, synchronous: int
) -> Iterator[PersistenceMetrics]:
    """Instrument all connections and override only the replay factory.

    NORMAL is the formal production replay setting. FULL is a benchmark-only
    control and cannot leak into the generic Database.connect API.
    """
    original_connect = sqlite3.connect
    original_replay_open = Database.open_reconstructible_replay_connection
    observed: list[int] = []

    def experimental_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        started = time.perf_counter_ns()
        kwargs["factory"] = _CountingConnection
        connection = cast(_CountingConnection, original_connect(*args, **kwargs))
        metrics.connections_opened += 1
        duration = time.perf_counter_ns() - started
        metrics.connect_durations_ns.append(duration)
        metrics.db_time_ns += duration
        connection.metrics = metrics
        return connection

    def experimental_replay_open(database: Database) -> sqlite3.Connection:
        connection = original_replay_open(database)
        connection.execute(f"PRAGMA synchronous={synchronous}")
        observed.append(int(connection.execute("PRAGMA synchronous").fetchone()[0]))
        return connection

    sqlite3.connect = experimental_connect  # type: ignore[assignment]
    Database.open_reconstructible_replay_connection = experimental_replay_open  # type: ignore[method-assign,assignment]
    try:
        yield metrics
    finally:
        Database.open_reconstructible_replay_connection = original_replay_open  # type: ignore[method-assign]
        sqlite3.connect = original_connect
        metrics.extra["effective_synchronous"] = (
            _MODE_NAMES.get(observed[0], str(observed[0])) if observed else "none"
        )
        metrics.extra["effective_synchronous_verified"] = bool(observed) and all(
            value == synchronous for value in observed
        )


def _replay_runner(candles_count: int) -> Any:
    from app.services.shadow_replay import run_shadow_replay

    def runner(metrics: PersistenceMetrics, synchronous: int) -> dict[str, Any]:
        workspace = Path(tempfile.mkdtemp(prefix=f"okx-dur-{candles_count}-"))
        try:
            candles = generate_synthetic_candles(
                SyntheticCandleRequest(count=candles_count, seed=_SYNTHETIC_SEED, bar_interval="1h")
            )
            csv_path = workspace / "replay.csv"
            save_candles_csv(candles, csv_path)
            database = Database(f"sqlite:///{workspace / 'replay.db'}")
            database.initialize()
            _seed_active_generation(database.path)
            config = load_run_config(_CONFIG, environ={})
            with experiment_sqlite(metrics, synchronous):
                result = run_shadow_replay(database, config, csv_path, candles_count)
            wal_path = database.path.with_name(f"{database.path.name}-wal")
            with sqlite3.connect(database.path) as observation:
                metrics.extra["wal_observation"] = {
                    "wal_size_bytes_after_clean_close": (
                        wal_path.stat().st_size if wal_path.exists() else 0
                    ),
                    "wal_autocheckpoint": int(
                        observation.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
                    ),
                    "journal_size_limit": int(
                        observation.execute("PRAGMA journal_size_limit").fetchone()[0]
                    ),
                    "checkpoint_stats": "not sampled: PASSIVE/FULL checkpoint would mutate WAL",
                }
            return {
                "candles": int(str(result["processed_candles"])),
                "buys": int(str(result["entry_signals"])),
                "proposals": int(str(result["shadow_proposals"])),
            }
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    return runner


def _seed_active_generation(database_path: Path) -> None:
    from tests.migration_fakes import _now

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO runtime_generations(generation_id,generation_number,status,
            created_at,activated_at,manifest_sha256,database_sha256_before,
            authorization_json,notes) VALUES ('gen-dur-0001',1,'active',?,?,'d','d',
            '{}','durability experiment')""",
            (_now(), _now()),
        )
        connection.commit()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return ordered[index]


def _variant(
    label: str,
    runner: Any,
    synchronous: int,
    repeats: int,
) -> dict[str, Any]:
    timings: list[float] = []
    commits_ms: list[float] = []
    connect_ms_total: list[float] = []
    sql_ms_total: list[float] = []
    commit_ms_total: list[float] = []
    first_counts: dict[str, Any] | None = None
    business: dict[str, Any] | None = None
    verified_flags: list[bool] = []
    effective_values: list[str] = []
    wal_observations: list[dict[str, Any]] = []

    for _ in range(repeats):
        metrics = PersistenceMetrics()
        started = time.perf_counter()
        result = runner(metrics, synchronous)
        timings.append(time.perf_counter() - started)
        counts = metrics.snapshot_counts()
        if first_counts is None:
            first_counts = counts
            business = result
        else:
            assert counts == first_counts, "non-deterministic counts invalidate the A/B"
        commits_ms.extend(value / 1_000_000 for value in metrics.commit_durations_ns)
        connect_ms_total.append(sum(metrics.connect_durations_ns) / 1_000_000)
        sql_ms_total.append(
            (
                metrics.db_time_ns
                - sum(metrics.commit_durations_ns)
                - sum(metrics.connect_durations_ns)
            )
            / 1_000_000
        )
        commit_ms_total.append(sum(metrics.commit_durations_ns) / 1_000_000)
        verified_flags.append(bool(metrics.extra.get("effective_synchronous_verified")))
        effective_values.append(str(metrics.extra.get("effective_synchronous")))
        wal_observations.append(dict(metrics.extra.get("wal_observation", {})))

    elapsed_median = statistics.median(timings)
    return {
        "variant": _MODE_NAMES[synchronous],
        "repeats": repeats,
        "counts": first_counts,
        "business": business,
        "effective_synchronous": effective_values[0],
        "effective_synchronous_verified_on_every_connection": all(verified_flags),
        "wal_observation": wal_observations[0],
        "elapsed_seconds": {
            "median": elapsed_median,
            "p95": _percentile(timings, 95),
            "min": min(timings),
            "max": max(timings),
        },
        "commit_ms": {
            "mean": statistics.mean(commits_ms) if commits_ms else 0.0,
            "median": statistics.median(commits_ms) if commits_ms else 0.0,
            "p95": _percentile(commits_ms, 95),
            "p99": _percentile(commits_ms, 99),
        },
        "cost_decomposition_ms_median": {
            "connect": statistics.median(connect_ms_total),
            "sql_statements": statistics.median(sql_ms_total),
            "commit": statistics.median(commit_ms_total),
            "other_python_strategy": max(
                0.0,
                elapsed_median * 1000
                - statistics.median(connect_ms_total)
                - statistics.median(sql_ms_total)
                - statistics.median(commit_ms_total),
            ),
        },
        "db_time_ms_median": statistics.median(commit_ms_total)
        + statistics.median(sql_ms_total)
        + statistics.median(connect_ms_total),
    }


# --------------------------------------------------- process-termination probe

_CRASH_CHILD = """
from pathlib import Path
import sqlite3, sys
from app.storage.database import Database

db, mode, phase, marker = sys.argv[1], int(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
database = Database(f"sqlite:///{db}")
database.initialize()
if mode == 1:
    connection = database.open_reconstructible_replay_connection()
else:
    connection = sqlite3.connect(db, timeout=30)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=2")
connection.execute("BEGIN IMMEDIATE")
connection.execute("CREATE TABLE IF NOT EXISTS crash_probe(id INTEGER PRIMARY KEY, tag TEXT)")
connection.execute("INSERT INTO crash_probe(tag) VALUES ('txn_row_1')")
connection.execute("INSERT INTO crash_probe(tag) VALUES ('txn_row_2')")
if phase == "before_commit":
    marker.write_text("waiting_uncommitted", encoding="ascii")
    sys.stdin.buffer.read()
else:
    connection.commit()
    marker.write_text("waiting_committed", encoding="ascii")
    sys.stdin.buffer.read()
"""


def process_termination_experiment() -> list[dict[str, Any]]:
    """Kill a child process before/after commit under both synchronous modes."""
    results: list[dict[str, Any]] = []
    for mode in (FULL, NORMAL):
        for phase in ("before_commit", "after_commit"):
            workspace = Path(tempfile.mkdtemp(prefix="okx-dur-crash-"))
            try:
                database = workspace / "crash.db"
                marker = workspace / "marker.txt"
                environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        _CRASH_CHILD,
                        str(database),
                        str(mode),
                        phase,
                        str(marker),
                    ],
                    cwd=_REPO_ROOT,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline and not marker.exists():
                        if child.poll() is not None:
                            raise RuntimeError(
                                f"crash child exited early: {child.stderr.read() if child.stderr else ''}"
                            )
                        time.sleep(0.02)
                    assert marker.exists(), "crash child never reached the wait point"
                finally:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)

                with sqlite3.connect(database) as connection:
                    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                    table_present = bool(
                        connection.execute(
                            "SELECT COUNT(*) FROM sqlite_master"
                            " WHERE type='table' AND name='crash_probe'"
                        ).fetchone()[0]
                    )
                    rows = (
                        int(connection.execute("SELECT COUNT(*) FROM crash_probe").fetchone()[0])
                        if table_present
                        else 0
                    )
                results.append(
                    {
                        "model": "process_termination",
                        "variant": _MODE_NAMES[mode],
                        "phase": phase,
                        "database_consistent": integrity == "ok",
                        "integrity_check": integrity,
                        "probe_table_present": table_present,
                        "probe_rows_after_kill": rows,
                        "committed_transaction_present": rows == 2,
                        "partial_transaction_present": rows == 1,
                    }
                )
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
    return results


# ----------------------------------------------------------------- experiment


def run_experiment(repeats_small: int = 10, directional_10000: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {
        "phase": "2B4",
        "generated_at": datetime.now(UTC).isoformat(),
        "workloads": {},
        "failure_models": {
            "process_termination": process_termination_experiment(),
            "python_exception_rollback": "covered by tests/test_durability_experiment_tools.py (both modes)",
            "os_crash": "NOT_TESTED (no infrastructure to flush OS page caches)",
            "power_loss": "NOT_TESTED (no disk/VM fault injection)",
        },
        "notes": [
            "NORMAL is the formal replay factory setting; FULL is benchmark-only override",
            "default and critical Database.connect paths remain FULL in both variants",
            "10000-candle run is a single directional repeat, not a statistical sample",
        ],
    }
    for count in (100, 1000):
        runner = _replay_runner(count)
        full = _variant(f"replay_{count}", runner, FULL, repeats_small)
        normal = _variant(f"replay_{count}", runner, NORMAL, repeats_small)
        full_counts = dict(full["counts"])
        normal_counts = dict(normal["counts"])
        assert full_counts == normal_counts, "FULL/NORMAL count mismatch invalidates A/B"
        assert full["business"] == normal["business"], "business result mismatch"
        full["throughput_candles_per_second"] = (
            full["business"]["candles"] / full["elapsed_seconds"]["median"]
        )
        normal["throughput_candles_per_second"] = (
            normal["business"]["candles"] / normal["elapsed_seconds"]["median"]
        )
        entry = {
            "candles": full["business"]["candles"],
            "buys": full["business"]["buys"],
            "counts_identical": full_counts == normal_counts,
            "business_results_identical": full["business"] == normal["business"],
            "FULL": full,
            "NORMAL": normal,
            "speedup": full["elapsed_seconds"]["median"] / normal["elapsed_seconds"]["median"],
        }
        report["workloads"][f"replay_{count}"] = entry

    if directional_10000:
        runner = _replay_runner(10_000)
        full = _variant("replay_10000", runner, FULL, 1)
        normal = _variant("replay_10000", runner, NORMAL, 1)
        assert full["counts"] == normal["counts"]
        report["workloads"]["replay_10000_directional_single_repeat"] = {
            "candles": full["business"]["candles"],
            "counts_identical": full["counts"] == normal["counts"],
            "FULL": full,
            "NORMAL": normal,
            "speedup": full["elapsed_seconds"]["median"] / normal["elapsed_seconds"]["median"],
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2B2 durability A/B experiment")
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPO_ROOT / "artifacts" / "performance" / "phase_2b4_durability_ab.json",
    )
    parser.add_argument("--skip-10000", action="store_true")
    args = parser.parse_args()

    started = time.perf_counter()
    report = run_experiment(directional_10000=not args.skip_10000)
    report["total_elapsed_seconds"] = time.perf_counter() - started
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\nartifact: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
