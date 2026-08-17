from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.legacy_quarantine import (
    LegacyLockQuarantineService,
    LegacyQuarantineDriftMonitor,
    LegacyRunQuarantineService,
    ProductionDatabaseAccessFromTestError,
    RuntimeGenerationGateError,
    RuntimeGenerationService,
    RuntimeStartupGate,
)
from app.storage.database import Database


def _database(tmp_path: Path) -> tuple[Database, datetime, str]:
    now = datetime(2026, 7, 27, tzinfo=UTC)
    database = Database(f"sqlite:///{tmp_path / 'quarantine.db'}")
    database.initialize()
    run_id = "legacy-run"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO continuous_demo_runs
            (run_id,strategy_name,instrument_id,timeframe,status,mode,configuration_hash,started_at,
             reconciliation_status,last_heartbeat_at,recovery_required)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "vwap",
                "BTC-USDT",
                "1m",
                "frozen",
                "shadow",
                "hash",
                (now - timedelta(days=1)).isoformat(),
                "unknown",
                None,
                1,
            ),
        )
        connection.execute(
            """INSERT INTO continuous_run_locks VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "continuous-demo",
                run_id,
                "other-host",
                999999,
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
                None,
                None,
            ),
        )
    return database, now, run_id


def _active_generation(database: Database, now: datetime) -> str:
    service = RuntimeGenerationService(database, now)
    generation_id = service.create_preparing("manifest", "database", {"authorized": True}, "test")
    service.activate(generation_id)
    return generation_id


def test_missing_heartbeat_is_a_quarantine_limitation_not_a_recovery_path(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    assessment = LegacyRunQuarantineService(database, now).assess(
        run_id, {"historical_evidence_found": False}
    )
    assert assessment.eligible
    assert assessment.heartbeat_state.value == "missing_unexpected"
    assert "heartbeat_missing_unexpected" not in assessment.blockers


def test_expired_heartbeat_is_historical_evidence_not_a_live_blocker(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE continuous_demo_runs SET last_heartbeat_at=? WHERE run_id=?",
            ((now - timedelta(minutes=1)).isoformat(), run_id),
        )

    assessment = LegacyRunQuarantineService(database, now).assess(
        run_id, {"historical_evidence_found": False}
    )

    assert assessment.heartbeat_state.value == "present_expired"
    assert assessment.eligible


def test_quarantine_is_record_only_and_lock_requires_run_record(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    generation_id = _active_generation(database, now)
    runs = LegacyRunQuarantineService(database, now)
    with pytest.raises(RuntimeGenerationGateError):
        LegacyLockQuarantineService(database, now).quarantine(run_id, generation_id, "manifest")
    assessment = runs.assess(run_id, {"historical_evidence_found": False})
    runs.quarantine(assessment, generation_id, "manifest", "snapshot", {"authorized": True})
    LegacyLockQuarantineService(database, now).quarantine(run_id, generation_id, "manifest")
    with database.connect() as connection:
        run = connection.execute(
            "SELECT status,last_heartbeat_at FROM continuous_demo_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        lock = connection.execute(
            "SELECT released_at FROM continuous_run_locks WHERE run_id=?", (run_id,)
        ).fetchone()
        assert tuple(run) == ("frozen", None)
        assert lock[0] is None


def test_order_or_exchange_evidence_blocks_quarantine(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO orders
            (client_order_id,instrument_id,side,order_type,quantity,price,signal_id,state,filled_quantity,created_at,updated_at,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    assessment = LegacyRunQuarantineService(database, now).assess(
        run_id, {"historical_evidence_found": True}
    )
    assert not assessment.eligible
    assert "local_order_present" in assessment.blockers
    assert "exchange_verification_not_clean" in assessment.blockers


def test_fully_excluded_external_activity_does_not_block_quarantine(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE continuous_demo_runs SET stop_reason='external_account_balance_change' WHERE run_id=?",
            (run_id,),
        )

    assessment = LegacyRunQuarantineService(database, now).assess(
        run_id,
        {
            "historical_evidence_found": False,
            "external_activity_fully_excluded": True,
        },
    )

    assert assessment.eligible


def test_windows_invalid_pid_is_not_treated_as_a_live_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_pid(_: int, __: int) -> None:
        error = OSError(87, "invalid parameter")
        error.winerror = 87
        raise error

    monkeypatch.setattr("app.services.legacy_quarantine.os.name", "nt")
    monkeypatch.setattr("app.services.legacy_quarantine.os.kill", invalid_pid)

    assert LegacyRunQuarantineService._process_exists(11648) is False


def test_local_external_activity_blocks_quarantine(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO continuous_external_activities
            (activity_id,run_id,activity_type,detected_at,source_endpoint,classification,severity,evidence_json)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                "activity",
                run_id,
                "order",
                now.isoformat(),
                "/api/v5/trade/orders-history",
                "external",
                "blocker",
                "{}",
            ),
        )
    assessment = LegacyRunQuarantineService(database, now).assess(
        run_id, {"historical_evidence_found": False}
    )
    assert not assessment.eligible
    assert "historical_external_activity_present" in assessment.blockers


def test_drift_blocks_startup_gate(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    generation_id = _active_generation(database, now)
    runs = LegacyRunQuarantineService(database, now)
    runs.quarantine(
        runs.assess(run_id, {"historical_evidence_found": False}),
        generation_id,
        "manifest",
        "snapshot",
        {},
    )
    LegacyLockQuarantineService(database, now).quarantine(run_id, generation_id, "manifest")
    assert RuntimeStartupGate(database).check((run_id,), run_id).allowed
    with database.connect() as connection:
        connection.execute(
            "UPDATE continuous_demo_runs SET status='changed' WHERE run_id=?", (run_id,)
        )
    assert LegacyQuarantineDriftMonitor(database, now).check_once(generation_id) == 1
    assert not RuntimeStartupGate(database).check((run_id,), run_id).allowed


def test_activation_rejects_incomplete_authorized_legacy_set(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    service = RuntimeGenerationService(database, now)
    generation_id = service.create_preparing(
        "manifest", "database", {"legacy_run_ids": [run_id], "legacy_lock_run_id": run_id}, "test"
    )
    with pytest.raises(RuntimeGenerationGateError, match="not fully quarantined"):
        service.activate(generation_id)


def test_activation_requires_auxiliary_lock_owner_quarantine(tmp_path: Path) -> None:
    database, now, run_id = _database(tmp_path)
    auxiliary_run = "lock-owner"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO continuous_demo_runs
            (run_id,strategy_name,instrument_id,timeframe,status,mode,configuration_hash,started_at,
             reconciliation_status,last_heartbeat_at,recovery_required)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                auxiliary_run,
                "vwap",
                "BTC-USDT",
                "1m",
                "stopped",
                "bounded_demo",
                "hash",
                now.isoformat(),
                "unknown",
                None,
                0,
            ),
        )
    service = RuntimeGenerationService(database, now)
    generation_id = service.create_preparing(
        "manifest",
        "database",
        {
            "legacy_run_ids": [run_id],
            "auxiliary_legacy_run_ids": [auxiliary_run],
            "legacy_lock_run_id": run_id,
        },
        "test",
    )
    assessment = LegacyRunQuarantineService(database, now).assess(
        run_id, {"historical_evidence_found": False}
    )
    LegacyRunQuarantineService(database, now).quarantine(
        assessment, generation_id, "manifest", "snapshot", {}
    )

    with pytest.raises(RuntimeGenerationGateError, match="not fully quarantined"):
        service.activate(generation_id)


def test_tests_cannot_target_the_production_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test")
    with pytest.raises(ProductionDatabaseAccessFromTestError):
        RuntimeGenerationService(Database("sqlite:///data/trading.db"))
