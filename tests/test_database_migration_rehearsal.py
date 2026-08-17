from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import app.storage.migrations as migrations_module
from app.config.run_config import load_run_config
from app.domain.private_state import PrivateStateStatus
from app.services.shadow_replay import run_shadow_replay
from app.storage.database import Database, StorageError
from app.storage.migrations import MIGRATIONS, Migration, MigrationError, MigrationManager
from app.storage.repositories import TradingRepository
from tests.migration_fakes import build_synthetic_v21_database

_ROOT = Path(__file__).resolve().parents[1]
_VWAP_LIVE_CSV = _ROOT / "tests" / "fixtures" / "vwap" / "btc_usdt_1h_live.csv"
_V21 = 21
_V22 = 22
_V23 = 23

_MIGRATION_CHILD = """
from pathlib import Path
import sys

import app.storage.migrations as migrations
from app.storage.migrations import MigrationManager

migrations.MIGRATIONS = migrations.MIGRATIONS[:22]
MigrationManager(Path(sys.argv[1])).migrate(backup=False)
Path(sys.argv[2]).write_text("v22-committed", encoding="ascii")
sys.stdin.buffer.read()
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_v21_database(tmp_path: Path, name: str) -> Path:
    """Build a deterministic synthetic v21 database (fresh-clone safe)."""
    destination = build_synthetic_v21_database(tmp_path / name)
    assert MigrationManager(destination).status().current_version == _V21
    return destination


def _migrate_to(path: Path, version: int, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    assert _V21 < version <= _V23
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS[:version])
    return MigrationManager(path).migrate(backup=False)


def _table_columns(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    tables = [
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'
            ORDER BY name"""
        )
    ]
    return {
        table: tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
        for table in tables
    }


def _business_digest(path: Path, columns: dict[str, tuple[str, ...]]) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    with sqlite3.connect(path) as connection:
        for table, names in columns.items():
            quoted_columns = ", ".join(f'"{name}"' for name in names)
            rows = connection.execute(
                f'SELECT {quoted_columns} FROM "{table}" ORDER BY {quoted_columns}'
            ).fetchall()
            payload = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
            result[table] = (len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return result


def _schema_fingerprint(path: Path) -> str:
    schema: list[dict[str, Any]] = []
    with sqlite3.connect(path) as connection:
        objects = connection.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name"""
        ).fetchall()
        for object_type, name, table, sql in objects:
            item: dict[str, Any] = {
                "type": object_type,
                "name": name,
                "table": table,
                "sql": sql,
            }
            if object_type == "table":
                item["columns"] = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
                item["foreign_keys"] = connection.execute(
                    f'PRAGMA foreign_key_list("{name}")'
                ).fetchall()
            if object_type == "index":
                item["index_columns"] = connection.execute(
                    f'PRAGMA index_info("{name}")'
                ).fetchall()
            schema.append(item)
    encoded = json.dumps(schema, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _integrity(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_v22_schema(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        control = connection.execute("PRAGMA table_info(private_state_control)").fetchall()
        proposal = connection.execute("PRAGMA table_info(demo_order_proposals)").fetchall()
    assert {row[1] for row in control} == {
        "control_id",
        "epoch",
        "version",
        "status",
        "last_consistent_at",
        "last_event_at",
        "dirty_reasons_json",
        "unknown_order_count",
        "updated_at",
    }
    assert {row[1] for row in proposal}.issuperset(
        {
            "private_state_epoch",
            "private_state_version",
            "fenced_private_state_version",
            "fenced_at",
        }
    )


def _assert_v23_schema(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        control = connection.execute("PRAGMA table_info(private_state_control)").fetchall()
        row = connection.execute(
            """SELECT epoch, version, status, ws_watermark
            FROM private_state_control WHERE control_id=1"""
        ).fetchone()
    columns = {item[1]: item for item in control}
    assert columns["ws_watermark"][3] == 1
    assert columns["ws_watermark"][4] == "0"
    assert row == (1, 0, "bootstrapping", 0)


def _fault_after_real_migration(migration: Migration) -> Migration:
    def apply(connection: sqlite3.Connection) -> None:
        migration.apply(connection)
        raise sqlite3.OperationalError("injected migration interruption")

    return replace(migration, apply=apply)


def _fault_before_real_migration(migration: Migration) -> Migration:
    def apply(_: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected pre-migration interruption")

    return replace(migration, apply=apply)


def _wait_for_marker(marker: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text(encoding="ascii") == "v22-committed":
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"migration child exited before v22 commit: {stderr}")
        time.sleep(0.01)
    raise AssertionError("migration child did not reach the v22 durable boundary")


def test_synthetic_v21_baseline_is_deterministic_and_schema_v21(tmp_path: Path) -> None:
    first = _copy_v21_database(tmp_path, "baseline-first-v21.db")
    second = _copy_v21_database(tmp_path, "baseline-second-v21.db")
    assert MigrationManager(first).status().current_version == _V21
    assert MigrationManager(second).status().current_version == _V21
    assert _schema_fingerprint(first) == _schema_fingerprint(second)


def test_v21_to_v22_preserves_existing_business_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_v21_database(tmp_path, "v22.db")
    with sqlite3.connect(copy) as connection:
        columns = _table_columns(connection)
    before = _business_digest(copy, columns)

    applied = _migrate_to(copy, _V22, monkeypatch)

    assert applied == (MIGRATIONS[_V21].name,)
    assert MigrationManager(copy).status().current_version == _V22
    _assert_v22_schema(copy)
    _integrity(copy)
    assert _business_digest(copy, columns) == before


def test_v22_to_v23_preserves_existing_business_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_v21_database(tmp_path, "v23-step.db")
    _migrate_to(copy, _V22, monkeypatch)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)
    with sqlite3.connect(copy) as connection:
        columns = _table_columns(connection)
    before = _business_digest(copy, columns)

    applied = MigrationManager(copy).migrate(backup=False)

    assert applied == (MIGRATIONS[_V22].name,)
    assert MigrationManager(copy).status().current_version == _V23
    _assert_v23_schema(copy)
    _integrity(copy)
    assert _business_digest(copy, columns) == before


def test_v21_to_v23_has_deterministic_schema_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _copy_v21_database(tmp_path, "v23-first.db")
    second = _copy_v21_database(tmp_path, "v23-second.db")
    with sqlite3.connect(first) as connection:
        columns = _table_columns(connection)
    before = _business_digest(first, columns)

    assert _migrate_to(first, _V23, monkeypatch) == tuple(
        migration.name for migration in MIGRATIONS[_V21:]
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)
    assert MigrationManager(second).migrate(backup=False) == tuple(
        migration.name for migration in MIGRATIONS[_V21:]
    )
    fingerprint = _schema_fingerprint(first)
    assert fingerprint == _schema_fingerprint(second)
    assert _business_digest(first, columns) == before
    before_rerun = _business_digest(first, columns)
    before_fingerprint = _schema_fingerprint(first)

    assert MigrationManager(first).migrate(backup=False) == ()

    assert MigrationManager(first).status().current_version == _V23
    assert _schema_fingerprint(first) == before_fingerprint
    assert _business_digest(first, columns) == before_rerun
    _integrity(first)


def test_v22_failure_before_work_keeps_the_copy_at_v21(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_v21_database(tmp_path, "failure-before-v22.db")
    faulty = _fault_before_real_migration(MIGRATIONS[_V21])
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (*MIGRATIONS[:_V21], faulty))

    with pytest.raises(MigrationError, match="事务已回滚"):
        MigrationManager(copy).migrate(backup=False)

    assert MigrationManager(copy).status().current_version == _V21
    with sqlite3.connect(copy) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_state_control'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("version", [_V22, _V23])
def test_real_migration_failure_rolls_back_then_restart_resumes(
    version: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_v21_database(tmp_path, f"failure-v{version}.db")
    if version == _V23:
        _migrate_to(copy, _V22, monkeypatch)
    original = MIGRATIONS[version - 1]
    faulty = _fault_after_real_migration(original)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (*MIGRATIONS[: version - 1], faulty))

    with pytest.raises(MigrationError, match="事务已回滚"):
        MigrationManager(copy).migrate(backup=False)

    status = MigrationManager(copy).status()
    assert status.current_version == version - 1
    assert original.name in status.failed
    with sqlite3.connect(copy) as connection:
        if version == _V22:
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_state_control'"
                ).fetchone()
                is None
            )
        else:
            assert "ws_watermark" not in {
                row[1] for row in connection.execute("PRAGMA table_info(private_state_control)")
            }

    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)
    assert MigrationManager(copy).migrate(backup=False) == tuple(
        migration.name for migration in MIGRATIONS[version - 1 :]
    )
    assert MigrationManager(copy).status().current_version == _V23
    _assert_v23_schema(copy)
    _integrity(copy)


def test_os_kill_after_v22_commit_restarts_same_copy_to_v23(tmp_path: Path) -> None:
    copy = _copy_v21_database(tmp_path, "os-restart.db")
    marker = tmp_path / "v22-committed.txt"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _MIGRATION_CHILD, str(copy), str(marker)],
        cwd=_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(marker, child)
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

    assert marker.read_text(encoding="ascii") == "v22-committed"
    assert MigrationManager(copy).status().current_version == _V22
    assert MigrationManager(copy).migrate(backup=False) == (MIGRATIONS[_V22].name,)
    assert MigrationManager(copy).status().current_version == _V23
    _assert_v23_schema(copy)
    _integrity(copy)


def test_old_and_future_schema_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _copy_v21_database(tmp_path, "old-v21.db")
    with pytest.raises(StorageError, match="db-migrate"):
        Database(f"sqlite:///{old}").initialize()

    future = _copy_v21_database(tmp_path, "future-v23.db")
    _migrate_to(future, _V23, monkeypatch)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)
    with sqlite3.connect(future) as connection:
        connection.execute(
            """INSERT INTO schema_migrations(version,name,checksum,applied_at,execution_status)
            VALUES (999,'future','fixture',?, 'successful')""",
            (datetime.now(UTC).isoformat(),),
        )
    with pytest.raises(StorageError, match="迁移校验失败"):
        Database(f"sqlite:///{future}").initialize()


def test_v23_copy_supports_local_private_state_and_vwap_shadow_without_broker_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy_v21_database(tmp_path, "compatibility-v23.db")
    _migrate_to(copy, _V23, monkeypatch)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)

    class BrokerSentinel:
        objects_created = 0
        write_calls = 0

        def __init__(self, *_: object, **__: object) -> None:
            type(self).objects_created += 1
            raise AssertionError("migration rehearsal must not construct a Broker")

        def submit_order(self, *_: object, **__: object) -> None:
            type(self).write_calls += 1
            raise AssertionError("migration rehearsal must not submit an order")

    import app.execution.backtest_broker as backtest_broker
    import app.execution.demo_broker as demo_broker
    import app.execution.read_only_broker as read_only_broker

    monkeypatch.setattr(backtest_broker, "BacktestBroker", BrokerSentinel)
    monkeypatch.setattr(demo_broker, "OKXDemoBroker", BrokerSentinel)
    monkeypatch.setattr(read_only_broker, "ReadOnlyBroker", BrokerSentinel)
    network_calls = 0

    def reject_network(*_: object, **__: object) -> None:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("migration rehearsal must not access the network")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    database = Database(f"sqlite:///{copy}")
    database.initialize()
    repository = TradingRepository(database)
    assert repository.private_state_snapshot().status is PrivateStateStatus.BOOTSTRAPPING
    repository.confirm_private_state_snapshots(datetime.now(UTC))
    assert repository.private_state_snapshot().status is PrivateStateStatus.HEALTHY
    config = load_run_config(_ROOT / "configs" / "btc_vwap_shadow.yaml", environ={})
    result = run_shadow_replay(
        database,
        config,
        _VWAP_LIVE_CSV,
        maximum=119,
    )

    assert result["status"] == "stopped"
    assert result["processed_candles"] == 119
    assert result["broker_objects_created"] == 0
    assert result["broker_write_calls"] == 0
    assert result["external_network_calls"] == 0
    assert BrokerSentinel.objects_created == 0
    assert BrokerSentinel.write_calls == 0
    assert network_calls == 0
    _integrity(copy)
