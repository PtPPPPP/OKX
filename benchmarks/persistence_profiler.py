"""Phase 2A persistence profiling entry point.

Runs four deterministic, offline workloads against temporary SQLite databases
and writes a machine-readable baseline artifact. No network, no broker writes,
no production data, no behavior change: instrumentation only wraps
``sqlite3.connect`` inside this process (see ``persistence_metrics``).

Usage:
    uv run python -m benchmarks.persistence_profiler [--quick] [--repeats 5]
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config.run_config import load_run_config
from app.domain.market import Candle
from app.market.historical_data import save_candles_csv
from app.market.synthetic_candles import (
    SyntheticCandleRequest,
    generate_synthetic_candles,
)
from app.services.vwap_shadow_soak import (
    build_synthetic_soak_source,
    load_csv_soak_source,
    run_vwap_shadow_soak,
)
from benchmarks.persistence_metrics import (
    PersistenceMetrics,
    instrumented_sqlite,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / "configs" / "btc_vwap_shadow.yaml"
_SYNTHETIC_SEED = 20260731


def _percent(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return ordered[index]


def _repeat_summary(values: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(values),
        "p95": _percent(values, 95),
        "min": min(values),
        "max": max(values),
    }


def _timed_repeats(label: str, repeats: int, runner: Any) -> dict[str, Any]:
    """Run a workload N times; int/str/dict counts must match, floats summarize.

    The runner returns a dict without wall-clock floats. Timing floats that the
    instrumentation derives (ms latencies, shares) are summarized as medians.
    """
    results: list[dict[str, Any]] = []
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = runner()
        timings.append(time.perf_counter() - started)
        results.append(result)
    first = results[0]
    count_keys = [key for key, value in first.items() if not isinstance(value, float)]
    deterministic = all(
        all(result.get(key) == first[key] for key in count_keys) for result in results[1:]
    )
    summary: dict[str, Any] = {
        "workload": label,
        "repeats": repeats,
        "deterministic_counts": deterministic,
        "elapsed_seconds_stats": _repeat_summary(timings),
    }
    for key, value in first.items():
        if isinstance(value, float):
            summary[key] = statistics.median([float(r[key]) for r in results])
        else:
            summary[key] = value
    return summary


def _base_metrics(metrics: PersistenceMetrics, **extra: Any) -> dict[str, Any]:
    return {**metrics.snapshot_counts(), **metrics.latency_summary(), **extra}


# ---------------------------------------------------------------- Workload A


def workload_candle_ingestion(candles_count: int) -> Any:
    """Soak engine (runtime connection reuse, per-bar transaction) over N candles."""

    def runner() -> dict[str, Any]:
        workspace = Path(tempfile.mkdtemp(prefix=f"okx-bench-a{candles_count}-"))
        metrics = PersistenceMetrics()
        try:
            config = load_run_config(_CONFIG, environ={})
            source = build_synthetic_soak_source(
                SyntheticCandleRequest(count=candles_count, seed=_SYNTHETIC_SEED, bar_interval="1h")
            )
            with instrumented_sqlite(metrics):
                result = run_vwap_shadow_soak(
                    database_path=workspace / "soak.db",
                    output_dir=workspace / "output",
                    config=config,
                    source=source,
                    bar_interval="1h",
                    checkpoint_every=1000,
                )
            return _base_metrics(
                metrics,
                confirmed_candles_processed=int(str(result["bars_processed"])),
                signals_generated=int(str(result["signals_persisted"])),
                buy_signals=int(str(result["buy_signals"])),
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    return runner


# ---------------------------------------------------------------- Workload B


def _region_candles(*, warmup: int, flat: int, drop_percent: str, tail: int) -> list[Candle]:
    """Deterministic closes: flat no-signal region followed by a drop region."""
    base = Decimal("30000")
    dropped = base * (Decimal("100") - Decimal(drop_percent)) / Decimal("100")
    closes = [base] * (warmup + flat) + [dropped] * tail
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("100"),
            confirmed=True,
        )
        for index, close in enumerate(closes)
    ]


def workload_vwap_signal_path() -> Any:
    """CSV soak over crafted no-signal and signal regions; per-signal write cost."""

    def runner() -> dict[str, Any]:
        workspace = Path(tempfile.mkdtemp(prefix="okx-bench-b-"))
        metrics = PersistenceMetrics()
        try:
            candles = _region_candles(warmup=24, flat=200, drop_percent="3", tail=200)
            csv_path = workspace / "regions.csv"
            save_candles_csv(candles, csv_path)
            config = load_run_config(_CONFIG, environ={})
            source = load_csv_soak_source(csv_path, bar_interval="1h")
            with instrumented_sqlite(metrics):
                result = run_vwap_shadow_soak(
                    database_path=workspace / "soak.db",
                    output_dir=workspace / "output",
                    config=config,
                    source=source,
                    bar_interval="1h",
                    checkpoint_every=len(candles),
                )
            buys = int(str(result["buy_signals"]))
            return _base_metrics(
                metrics,
                confirmed_candles_processed=int(str(result["bars_processed"])),
                signals_generated=int(str(result["signals_persisted"])),
                buy_signals=buys,
                writes_per_signal=(metrics.sql_writes / buys if buys else None),
                commits_per_signal=(metrics.transactions_committed / buys if buys else None),
                connections_per_signal=(metrics.connections_opened / buys if buys else None),
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    return runner


# ---------------------------------------------------------------- Workload C


def _seed_active_generation(database_path: Path) -> None:
    """run_shadow_replay requires exactly one active runtime generation."""
    from tests.migration_fakes import _now

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO runtime_generations(generation_id, generation_number, status,
            created_at, activated_at, manifest_sha256, database_sha256_before,
            authorization_json, notes)
            VALUES ('gen-bench-0001', 1, 'active', ?, ?, 'bench-manifest-sha',
            'bench-database-sha', '{"operator": "phase2a-benchmark"}',
            'synthetic benchmark generation')""",
            (_now(), _now()),
        )
        connection.commit()


def workload_legacy_shadow_replay(candles_count: int) -> Any:
    """run_shadow_replay: per-candle multi-connection research/test path."""

    def runner() -> dict[str, Any]:
        from app.services.shadow_replay import run_shadow_replay
        from app.storage.database import Database

        workspace = Path(tempfile.mkdtemp(prefix=f"okx-bench-c{candles_count}-"))
        metrics = PersistenceMetrics()
        try:
            candles = generate_synthetic_candles(
                SyntheticCandleRequest(count=candles_count, seed=_SYNTHETIC_SEED, bar_interval="1h")
            )
            csv_path = workspace / "replay.csv"
            save_candles_csv(candles, csv_path)
            database = Database(f"sqlite:///{workspace / 'replay.db'}")
            database.initialize()  # migration cost excluded from workload metrics
            _seed_active_generation(database.path)
            config = load_run_config(_CONFIG, environ={})
            with instrumented_sqlite(metrics):
                result = run_shadow_replay(database, config, csv_path, candles_count)
            return _base_metrics(
                metrics,
                confirmed_candles_processed=int(str(result["processed_candles"])),
                signals_generated=int(str(result["strategy_evaluations"])),
                buy_signals=int(str(result["entry_signals"])),
                writes_per_candle=metrics.sql_writes / candles_count,
                commits_per_candle=metrics.transactions_committed / candles_count,
                connections_per_candle=metrics.connections_opened / candles_count,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    return runner


# ---------------------------------------------------------------- Workload D


def workload_order_lifecycle() -> Any:
    """Critical order-path writes: prepared→fenced→started→submitted→revalidated.

    Uses the same offline safety harness as the demo write boundary tests.
    Counts and transaction boundaries only, not throughput.
    """

    def runner() -> dict[str, Any]:
        from app.services.controlled_demo_write import ControlledDemoWriteService
        from app.services.demo_order_preflight import ProposalStatus
        from tests.test_demo_write_boundary import FakeWriteClient, _controlled_order

        workspace = Path(tempfile.mkdtemp(prefix="okx-bench-d-"))
        metrics = PersistenceMetrics()
        try:
            with instrumented_sqlite(metrics):
                repository, local = _controlled_order(workspace)
                client = FakeWriteClient()
                service = ControlledDemoWriteService(repository, client)
                placed = service.place_order(local)
                repository.complete_controlled_demo_submission(
                    local.request.signal_id,
                    placed,
                    event_type="submitted",
                    proposal_status=ProposalStatus.SUBMITTED,
                )
                repository.save_revalidation_event(
                    local.request.signal_id, "revalidation_started", "bench"
                )
                repository.save_revalidation_event(
                    local.request.signal_id, "revalidation_passed", "bench"
                )
            return _base_metrics(
                metrics,
                note="counts_only_critical_order_path_not_throughput",
                critical_path_writes=(
                    metrics.sql_writes - metrics.table_writes.get("schema_migrations", 0)
                ),
                critical_path_table_writes={
                    table: count
                    for table, count in metrics.table_writes.items()
                    if table != "schema_migrations"
                },
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    return runner


# ------------------------------------------------------------- Query plans


_HOT_QUERIES = (
    (
        "processed_candles_pk",
        "SELECT * FROM processed_candles WHERE run_id=? AND instrument_id=? AND timeframe=? AND candle_open_time=?",
    ),
    (
        "strategy_runtime_states_upsert",
        "SELECT * FROM strategy_runtime_states WHERE run_id=? AND strategy_name=? AND instrument_id=? AND timeframe=?",
    ),
    (
        "strategy_signal_events_unique",
        "SELECT * FROM strategy_signal_events WHERE run_id=? AND instrument_id=? AND candle_open_time=? AND signal_type=?",
    ),
    ("shadow_order_proposals_by_run", "SELECT * FROM shadow_order_proposals WHERE run_id=?"),
    ("continuous_demo_runs_by_run", "SELECT * FROM continuous_demo_runs WHERE run_id=?"),
    ("orders_by_client_order_id", "SELECT * FROM orders WHERE client_order_id=?"),
    ("demo_order_proposals_by_id", "SELECT * FROM demo_order_proposals WHERE proposal_id=?"),
    (
        "signals_by_timestamp_range",
        "SELECT * FROM signals WHERE instrument_id=? AND timestamp BETWEEN ? AND ?",
    ),
    ("private_state_snapshots_scope", "SELECT * FROM private_state_snapshots WHERE scope_key=?"),
)


def query_plan_findings() -> list[dict[str, str]]:
    workspace = Path(tempfile.mkdtemp(prefix="okx-bench-plan-"))
    try:
        from app.services.shadow_replay import run_shadow_replay
        from app.storage.database import Database

        candles = generate_synthetic_candles(
            SyntheticCandleRequest(count=60, seed=_SYNTHETIC_SEED, bar_interval="1h")
        )
        csv_path = workspace / "plan.csv"
        save_candles_csv(candles, csv_path)
        database = Database(f"sqlite:///{workspace / 'plan.db'}")
        database.initialize()
        _seed_active_generation(database.path)
        run_shadow_replay(database, load_run_config(_CONFIG, environ={}), csv_path, 60)
        findings: list[dict[str, str]] = []
        with sqlite3.connect(workspace / "plan.db") as connection:
            for name, sql in _HOT_QUERIES:
                try:
                    plan_rows = connection.execute(
                        f"EXPLAIN QUERY PLAN {sql}", ("x",) * sql.count("?")
                    ).fetchall()
                    detail = "; ".join(str(row[-1]) for row in plan_rows)
                except sqlite3.Error as exc:
                    detail = f"ERROR: {exc}"
                findings.append({"query": name, "plan": detail})
        return findings
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ----------------------------------------------------------------- plumbing


def environment() -> dict[str, str]:
    workspace = Path(tempfile.mkdtemp(prefix="okx-bench-env-"))
    try:
        connection = sqlite3.connect(workspace / "env.db")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            synchronous = str(connection.execute("PRAGMA synchronous").fetchone()[0])
        finally:
            connection.close()
        return {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "journal_mode": journal,
            "synchronous": synchronous,
            "os": f"{platform.system()} {platform.release()}",
            "cpu": platform.processor() or "unknown",
            "filesystem": "local NTFS (workspace volume)",
            "db_location": "tempfile.TemporaryDirectory on local disk",
            "process_count": "1",
            "note": "dev workstation, single process; absolute timings not for capacity planning",
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2A persistence profiling")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--quick", action="store_true", help="smaller workloads")
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPO_ROOT / "artifacts" / "performance" / "phase_2a_baseline.json",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    scales = (100, 1000) if args.quick else (100, 1000, 10000)
    report: dict[str, Any] = {
        "phase": "2A",
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": environment(),
        "workloads": {},
        "query_plans": query_plan_findings(),
    }
    for count in scales:
        summary = _timed_repeats(
            f"A_candle_ingestion_{count}", args.repeats, workload_candle_ingestion(count)
        )
        candles = int(summary["confirmed_candles_processed"])
        median_elapsed = summary["elapsed_seconds_stats"]["median"]
        summary["throughput_candles_per_second"] = candles / median_elapsed
        summary["writes_per_candle"] = summary["sql_writes"] / candles
        summary["commits_per_candle"] = summary["commit_calls"] / candles
        summary["connections_per_candle"] = summary["connections_opened"] / candles
        summary["db_time_share_percent"] = round(
            100 * summary["db_time_ms"] / (median_elapsed * 1000), 2
        )
        report["workloads"][f"A_{count}"] = summary

    report["workloads"]["B"] = _timed_repeats(
        "B_vwap_signal_path", args.repeats, workload_vwap_signal_path()
    )
    replay_count = 100 if args.quick else 1000
    report["workloads"][f"C_{replay_count}"] = _timed_repeats(
        f"C_legacy_shadow_replay_{replay_count}",
        args.repeats,
        workload_legacy_shadow_replay(replay_count),
    )
    report["workloads"]["D"] = _timed_repeats(
        "D_order_lifecycle", args.repeats, workload_order_lifecycle()
    )

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
