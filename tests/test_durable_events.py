from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.events import DomainEvent, EventBus
from app.storage.audit import AuditRepository
from app.storage.database import Database, StorageError


def _event(key: str) -> DomainEvent:
    return DomainEvent(
        "run",
        datetime(2026, 1, 1, tzinfo=UTC),
        "signal",
        "BTC-USDT",
        "test",
        {"n": key},
        idempotency_key=key,
    )


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'events.db'}")
    database.initialize()
    return database


def test_accelerated_event_stream_has_bounded_memory_and_complete_audit(tmp_path: Path) -> None:
    database = _database(tmp_path)
    bus = EventBus(AuditRepository(database), retention_limit=32)
    for n in range(1500):
        assert bus.publish(_event(str(n)))
    assert len(bus.events) == len(bus._seen) == 32
    assert not bus.publish(_event("0"))  # Evicted keys still deduplicate on disk.
    conflicting = _event("0")
    conflicting.payload["n"] = "changed"
    with pytest.raises(ValueError, match="不同负载"):
        bus.publish(conflicting)
    restarted = EventBus(
        AuditRepository(Database(f"sqlite:///{database.path}")), retention_limit=32
    )
    assert not restarted.publish(_event("1499"))
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE record_type='domain_event'"
            ).fetchone()[0]
            == 1500
        )
        assert connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 1500
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_disk_failure_does_not_claim_or_publish_unpersisted_event(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_records BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
    bus = EventBus(AuditRepository(database), retention_limit=2)
    with pytest.raises(StorageError, match="injected audit failure"):
        bus.publish(_event("critical"))
    assert not bus.events and not bus._seen
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_audit")
    assert bus.publish(_event("critical"))


def test_process_exit_during_audit_transaction_leaves_no_partial_claim(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER crash_audit BEFORE INSERT ON audit_records BEGIN SELECT local_crash(); END"
        )
    script = r"""
import os,sys
from datetime import UTC,datetime
from app.domain.events import DomainEvent
from app.storage.database import Database
from app.storage.audit import AuditRepository
database=Database('sqlite:///'+sys.argv[1])
original=database._open_connection
def opening(durability):
    connection=original(durability)
    connection.create_function('local_crash',0,lambda:os._exit(23))
    return connection
database._open_connection=opening
AuditRepository(database).append_event(DomainEvent('run',datetime(2026,1,1,tzinfo=UTC),'critical','BTC-USDT','test',{},idempotency_key='crash'))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(database.path)],
        check=False,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 23, result.stderr.decode(errors="replace")
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0] == 0
        connection.execute("DROP TRIGGER crash_audit")


@pytest.mark.parametrize("limit", [0, -1, 1])
def test_bounded_mode_requires_reliable_store(limit: int) -> None:
    with pytest.raises(ValueError, match="durable"):
        EventBus(retention_limit=limit)


def test_complete_identity_collision_is_rejected_even_in_memory(tmp_path: Path) -> None:
    from dataclasses import replace

    bus = EventBus(AuditRepository(_database(tmp_path)), retention_limit=2)
    event = _event("id")
    bus.publish(event)
    with pytest.raises(ValueError, match="身份"):
        bus.publish(replace(event, run_id="different-run"))


def test_streaming_audit_retains_run_mode_and_bar(tmp_path: Path) -> None:
    database = _database(tmp_path)
    bus = EventBus(AuditRepository(database, event_mode="demo", event_bar="1h"), retention_limit=2)
    bus.publish(_event("identity"))
    with database.connect() as connection:
        row = connection.execute("SELECT mode,bar FROM audit_records").fetchone()
    assert tuple(row) == ("demo", "1h")


def test_session_initial_audit_failure_still_closes_resources(btc_instrument) -> None:
    from types import SimpleNamespace

    from app.session import SessionDescriptor, TradingSession

    closed: list[bool] = []

    class Repository:
        def save_audit_record(self, **_):
            raise StorageError("audit unavailable")

    session = TradingSession(
        SessionDescriptor("demo", "test", "BTC-USDT", "1h", {}),
        btc_instrument,
        SimpleNamespace(run_id="run"),
        Repository(),
        EventBus(),
        resources=(SimpleNamespace(close=lambda: closed.append(True)),),
    )
    with pytest.raises(StorageError, match="audit unavailable"):
        session.run()
    assert closed == [True]
