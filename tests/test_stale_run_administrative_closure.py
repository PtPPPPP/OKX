from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.stale_run_administrative_closure import StaleRunAdministrativeClosureService
from app.storage.database import Database


def _database(tmp_path: Path) -> tuple[Database, str, datetime]:
    database = Database(f"sqlite:///{tmp_path / 'closure.db'}")
    database.initialize()
    now = datetime(2026, 7, 26, tzinfo=UTC)
    run_id = "stale-run"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO continuous_demo_runs
            (run_id,strategy_name,instrument_id,timeframe,status,mode,configuration_hash,started_at,
             reconciliation_status,last_heartbeat_at,recovery_required)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "vwap_mean_reversion",
                "BTC-USDT",
                "5m",
                "recovery_required",
                "shadow",
                "config",
                (now - timedelta(hours=2)).isoformat(),
                "unknown",
                (now - timedelta(hours=1)).isoformat(),
                1,
            ),
        )
        connection.execute(
            """INSERT INTO continuous_run_locks
            VALUES ('continuous-demo',?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "other-host",
                999999,
                (now - timedelta(hours=2)).isoformat(),
                (now - timedelta(hours=2)).isoformat(),
                (now - timedelta(hours=1)).isoformat(),
                None,
                None,
            ),
        )
    return database, run_id, now


def test_administrative_closure_preserves_missing_baseline_limitation(tmp_path: Path) -> None:
    database, run_id, now = _database(tmp_path)
    result = StaleRunAdministrativeClosureService(database, now).close(run_id, "missing_baseline")
    assert result.closed
    with database.connect() as connection:
        run = connection.execute(
            "SELECT status,reconciliation_status FROM continuous_demo_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        recovery = connection.execute(
            "SELECT closure_type,historical_baseline_available,reconciliation_status FROM continuous_run_recoveries WHERE run_id=?",
            (run_id,),
        ).fetchone()
        lock = connection.execute(
            "SELECT released_at,release_reason FROM continuous_run_locks WHERE run_id=?", (run_id,)
        ).fetchone()
    assert tuple(run) == ("administratively_closed", "incomplete")
    assert tuple(recovery) == ("administrative_stale_closure", 0, "incomplete")
    assert lock[0] is not None and lock[1] == "administrative_stale_closure"


def test_administrative_closure_rejects_run_with_order(tmp_path: Path) -> None:
    database, run_id, now = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO orders (client_order_id,instrument_id,side,order_type,quantity,price,signal_id,state,filled_quantity,created_at,updated_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "order",
                "BTC-USDT",
                "buy",
                "limit",
                "1",
                "1",
                "signal",
                "created",
                "0",
                now.isoformat(),
                now.isoformat(),
                run_id,
            ),
        )
    result = StaleRunAdministrativeClosureService(database, now).close(run_id, "missing_baseline")
    assert not result.closed
    assert "local_order_present" in result.blockers


def test_missing_heartbeat_is_not_mapped_to_not_expired(tmp_path: Path) -> None:
    database, run_id, now = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE continuous_demo_runs SET last_heartbeat_at=NULL WHERE run_id=?", (run_id,)
        )
    result = StaleRunAdministrativeClosureService(database, now).close(run_id, "missing_baseline")
    assert not result.closed
    assert "heartbeat_missing_unexpected" in result.blockers
    assert "heartbeat_not_expired" not in result.blockers
