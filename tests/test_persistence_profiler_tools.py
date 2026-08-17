"""Tests for the Phase 2A benchmark instrumentation itself.

No timing assertions: only that counters are correct, the instrumentation is
removed afterwards, and that instrumented and plain runs produce identical
business results (zero semantic effect).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.synthetic_candles import SyntheticCandleRequest
from app.services.vwap_shadow_soak import (
    build_synthetic_soak_source,
    run_vwap_shadow_soak,
)
from benchmarks.persistence_metrics import (
    PersistenceMetrics,
    classify,
    instrumented_sqlite,
    target_table,
)


def test_statement_classification_and_table_extraction() -> None:
    assert classify("INSERT INTO orders VALUES (1)") == "INSERT"
    assert classify("insert or ignore into fills values (1)") == "INSERT"
    assert classify("  UPDATE orders SET state='x'") == "UPDATE"
    assert classify("DELETE FROM t") == "DELETE"
    assert classify("SELECT * FROM t") == "SELECT"
    assert classify("BEGIN IMMEDIATE") == "BEGIN"
    assert classify("PRAGMA foreign_keys=ON") == "PRAGMA"
    assert classify("-- comment\nUPDATE t SET x=1") == "UPDATE"
    assert target_table("INSERT OR IGNORE INTO processed_candles VALUES (1)") == (
        "processed_candles"
    )
    assert target_table("UPDATE continuous_demo_runs SET status='x'") == ("continuous_demo_runs")
    assert target_table("DELETE FROM signals WHERE id=1") == "signals"
    assert target_table("SELECT * FROM signals") is None


def test_instrumented_sqlite_counts_and_restores(tmp_path: Path) -> None:
    database = tmp_path / "counted.db"
    metrics = PersistenceMetrics()
    original_connect = sqlite3.connect

    with instrumented_sqlite(metrics):
        assert sqlite3.connect is not original_connect
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany("INSERT INTO t(value) VALUES (?)", [("a",), ("b",)])
        connection.execute("UPDATE t SET value='c' WHERE id=1")
        connection.execute("SELECT * FROM t").fetchall()
        connection.commit()
        connection.execute("INSERT INTO t(value) VALUES ('d')")
        connection.rollback()
        connection.close()

    assert sqlite3.connect is original_connect
    assert metrics.connections_opened == 1
    assert metrics.statements["CREATE"] == 1
    assert metrics.statements["INSERT"] == 2  # executemany counts as one statement
    assert metrics.statements["UPDATE"] == 1
    assert metrics.statements["SELECT"] == 1
    assert metrics.sql_writes == 3
    assert metrics.sql_reads == 1
    assert metrics.commit_calls == 1
    assert metrics.rollback_calls == 1
    assert metrics.table_writes["t"] == 3
    assert metrics.statement_durations_ns
    assert metrics.commit_durations_ns
    assert metrics.db_time_ns > 0
    # plain sqlite still works after restore
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_instrumentation_does_not_change_business_results(tmp_path: Path) -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    source = build_synthetic_soak_source(
        SyntheticCandleRequest(count=120, seed=7, bar_interval="1h")
    )

    plain = run_vwap_shadow_soak(
        database_path=tmp_path / "plain.db",
        output_dir=tmp_path / "plain-out",
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=60,
    )
    metrics = PersistenceMetrics()
    with instrumented_sqlite(metrics):
        instrumented = run_vwap_shadow_soak(
            database_path=tmp_path / "instr.db",
            output_dir=tmp_path / "instr-out",
            config=config,
            source=source,
            bar_interval="1h",
            checkpoint_every=60,
        )

    for key in (
        "status",
        "bars_received",
        "bars_confirmed",
        "bars_processed",
        "signals_persisted",
        "buy_signals",
        "proposals_persisted",
        "checkpoint_count",
    ):
        assert plain[key] == instrumented[key]
    assert metrics.connections_opened > 0
    assert metrics.commit_calls >= int(str(plain["bars_processed"]))


def test_latency_summary_reports_ordered_percentiles() -> None:
    metrics = PersistenceMetrics()
    for value in (10, 20, 30, 40, 100):
        metrics.statement_durations_ns.append(value * 1_000_000)
    summary = metrics.latency_summary()
    assert summary["p50_db_operation_ms"] == 30.0
    assert summary["p95_db_operation_ms"] == 100.0
    assert summary["mean_db_operation_ms"] == 40.0
