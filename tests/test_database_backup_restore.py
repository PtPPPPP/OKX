from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.storage.database import Database, StorageError
from app.storage.database_backup import (
    DatabaseBackupError,
    backup_manifest_path,
    business_data_digests,
    create_verified_backup,
    file_identity,
    restore_evidence,
    restore_verified_backup,
    verify_backup,
)
from app.storage.migrations import MigrationManager
from app.storage.repositories import TradingRepository
from tests.migration_fakes import build_synthetic_v21_database

_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_DATABASE = _ROOT / "data" / "trading.db"


@pytest.fixture
def v21_database(tmp_path: Path) -> Path:
    return build_synthetic_v21_database(tmp_path / "synthetic-v21.db")


_INTERRUPTED_BACKUP_CHILD = """
from pathlib import Path
import os
import sys

import app.storage.database_backup as backup_module

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
marker = Path(sys.argv[3])

def interrupted_copy(source_path, destination_path):
    with Path(source_path).open('rb') as input_file, Path(destination_path).open('wb') as output_file:
        output_file.write(input_file.read(4096))
        output_file.flush()
    pending_marker = marker.with_suffix('.pending')
    pending_marker.write_text('partial-copy-created', encoding='ascii')
    os.replace(pending_marker, marker)
    sys.stdin.buffer.read()

backup_module.shutil.copyfile = interrupted_copy
backup_module.create_verified_backup(source, destination)
"""


def _wait_for_marker(marker: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"backup child exited before partial copy: {stderr}")
        time.sleep(0.01)
    raise AssertionError("backup child did not reach partial-copy boundary")


def test_verified_backup_manifest_and_database_checks_use_only_production_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, v21_database: Path
) -> None:
    class BrokerSentinel:
        objects_created = 0

        def __init__(self, *_: object, **__: object) -> None:
            type(self).objects_created += 1
            raise AssertionError("backup must not construct a Broker")

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
        raise AssertionError("backup must not access the network")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    before = file_identity(v21_database)
    backup = tmp_path / "trading-v21.db"

    manifest = create_verified_backup(v21_database, backup)
    verification = verify_backup(backup)
    after = file_identity(v21_database)

    assert before == after
    assert verification.valid
    assert manifest.source_sha256 == manifest.backup_sha256 == before.sha256
    assert manifest.source_size == manifest.backup_size == before.size
    assert manifest.source_schema_version == 21
    assert manifest.integrity_check == "ok"
    assert manifest.foreign_key_violations == 0
    assert backup_manifest_path(backup).is_file()
    assert BrokerSentinel.objects_created == 0
    assert network_calls == 0


def test_restore_matches_verified_backup_business_data_and_can_migrate_to_v23(
    tmp_path: Path, v21_database: Path
) -> None:
    backup = tmp_path / "backup.db"
    create_verified_backup(v21_database, backup)
    restored = tmp_path / "restored-v21.db"
    published = restore_verified_backup(backup, restored, production_path=_PRODUCTION_DATABASE)
    evidence = restore_evidence(backup, restored)

    assert published.source_sha256 == published.restored_sha256
    assert evidence.valid
    assert business_data_digests(backup) == business_data_digests(restored)
    assert MigrationManager(restored).status().current_version == 21
    with pytest.raises(StorageError, match="db-migrate"):
        Database(f"sqlite:///{restored}").initialize()

    working = tmp_path / "restored-working.db"
    shutil.copyfile(restored, working)
    assert MigrationManager(working).migrate(backup=False)
    assert MigrationManager(working).status().current_version == 23
    database = Database(f"sqlite:///{working}")
    database.initialize()
    assert TradingRepository(database).private_state_snapshot().ws_watermark == 0


def test_migration_failure_working_copy_can_be_discarded_and_restored_to_v21(
    tmp_path: Path, v21_database: Path
) -> None:
    backup = tmp_path / "backup.db"
    create_verified_backup(v21_database, backup)
    working = tmp_path / "working-v23.db"
    restore_verified_backup(backup, working, production_path=_PRODUCTION_DATABASE)
    assert MigrationManager(working).migrate(backup=False)
    with sqlite3.connect(working) as connection:
        connection.execute("CREATE TABLE temporary_migration_invalidation(id INTEGER PRIMARY KEY)")

    restored = tmp_path / "rollback-v21.db"
    restore_verified_backup(backup, restored, production_path=_PRODUCTION_DATABASE)
    evidence = restore_evidence(backup, restored)

    assert evidence.valid
    assert MigrationManager(restored).status().current_version == 21
    assert business_data_digests(backup) == business_data_digests(restored)


def test_backup_and_restore_refuse_unsafe_or_invalid_paths(
    tmp_path: Path, v21_database: Path
) -> None:
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"existing")
    with pytest.raises(DatabaseBackupError, match="拒绝覆盖"):
        create_verified_backup(v21_database, existing)
    with pytest.raises(DatabaseBackupError, match="不存在"):
        create_verified_backup(tmp_path / "missing.db", tmp_path / "backup.db")
    temporary_source = tmp_path / "temporary-source.db"
    temporary_source.write_bytes(b"temporary")
    with pytest.raises(DatabaseBackupError, match="禁止写入正式数据库路径"):
        create_verified_backup(temporary_source, _PRODUCTION_DATABASE)

    backup = tmp_path / "verified.db"
    create_verified_backup(v21_database, backup)
    with pytest.raises(DatabaseBackupError, match="禁止通过 SQLite 打开正式数据库"):
        business_data_digests(_PRODUCTION_DATABASE)
    with pytest.raises(DatabaseBackupError, match="禁止覆盖正式数据库"):
        restore_verified_backup(backup, _PRODUCTION_DATABASE, production_path=_PRODUCTION_DATABASE)
    with pytest.raises(DatabaseBackupError, match="禁止覆盖正式数据库"):
        restore_verified_backup(backup, _PRODUCTION_DATABASE, production_path=tmp_path / "other.db")
    with pytest.raises(DatabaseBackupError, match="不能覆盖备份"):
        restore_verified_backup(backup, backup, production_path=_PRODUCTION_DATABASE)
    existing_restore = tmp_path / "existing-restore.db"
    existing_restore.write_bytes(b"existing")
    with pytest.raises(DatabaseBackupError, match="恢复目标已存在"):
        restore_verified_backup(backup, existing_restore, production_path=_PRODUCTION_DATABASE)


def test_corrupted_or_manifest_tampered_backup_fails_closed(
    tmp_path: Path, v21_database: Path
) -> None:
    backup = tmp_path / "verified.db"
    create_verified_backup(v21_database, backup)
    with backup.open("r+b") as file:
        file.seek(0)
        file.write(b"not-a-valid-sqlite-header")

    with pytest.raises(DatabaseBackupError):
        verify_backup(backup)
    with pytest.raises(DatabaseBackupError):
        restore_verified_backup(
            backup, tmp_path / "restored.db", production_path=_PRODUCTION_DATABASE
        )

    manifest_backup = tmp_path / "manifest-tampered.db"
    create_verified_backup(v21_database, manifest_backup)
    manifest_path = backup_manifest_path(manifest_backup)
    manifest_path.write_text("{}", encoding="utf-8")
    with pytest.raises(DatabaseBackupError, match="manifest"):
        verify_backup(manifest_backup)


def test_partial_copy_is_not_published_as_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, v21_database: Path
) -> None:
    def partial_copy(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> str:
        with Path(source).open("rb") as input_file, Path(destination).open("wb") as output_file:
            output_file.write(input_file.read(4096))
        return str(destination)

    monkeypatch.setattr(shutil, "copyfile", partial_copy)
    destination = tmp_path / "partial.db"
    with pytest.raises(DatabaseBackupError, match="字节校验"):
        create_verified_backup(v21_database, destination)

    assert not destination.exists()
    assert not backup_manifest_path(destination).exists()


def test_foreign_key_failure_or_restore_hash_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, v21_database: Path
) -> None:
    invalid_source = tmp_path / "foreign-key-invalid.db"
    with sqlite3.connect(invalid_source) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(parent_id INTEGER NOT NULL REFERENCES parent(id))")
        connection.execute("INSERT INTO child(parent_id) VALUES (999)")
    with pytest.raises(DatabaseBackupError, match="完整性校验失败"):
        create_verified_backup(invalid_source, tmp_path / "invalid-backup.db")

    backup = tmp_path / "verified.db"
    create_verified_backup(v21_database, backup)
    original_copyfile = shutil.copyfile

    def mismatched_restore(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> str:
        result = original_copyfile(source, destination)
        with Path(destination).open("r+b") as file:
            file.seek(0)
            file.write(b"mismatch")
        return str(result)

    monkeypatch.setattr(shutil, "copyfile", mismatched_restore)
    restored = tmp_path / "hash-mismatch.db"
    with pytest.raises(DatabaseBackupError, match="恢复字节校验失败"):
        restore_verified_backup(backup, restored, production_path=_PRODUCTION_DATABASE)
    assert not restored.exists()


def test_os_kill_during_backup_leaves_only_rejected_partial_file(
    tmp_path: Path, v21_database: Path
) -> None:
    destination = tmp_path / "interrupted.db"
    marker = tmp_path / "partial-marker.txt"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            _INTERRUPTED_BACKUP_CHILD,
            str(v21_database),
            str(destination),
            str(marker),
        ],
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

    partials = list(tmp_path.glob(".interrupted.db.*.partial"))
    assert marker.read_text(encoding="ascii") == "partial-copy-created"
    assert len(partials) == 1
    assert not destination.exists()
    with pytest.raises(DatabaseBackupError):
        verify_backup(destination)
    with pytest.raises(DatabaseBackupError):
        verify_backup(partials[0])


def test_create_verified_backup_rejects_uncheckpointed_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    Database(f"sqlite:///{source}").initialize()
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE wal_probe(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO wal_probe VALUES (1)")
        connection.commit()
        wal = tmp_path / "source.db-wal"
        assert wal.is_file() and wal.stat().st_size > 0
        with pytest.raises(DatabaseBackupError, match="WAL"):
            create_verified_backup(source, tmp_path / "backup.db")
    finally:
        connection.close()


def test_restore_verified_backup_reports_real_business_digests(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    Database(f"sqlite:///{source}").initialize()
    backup = tmp_path / "backup.db"
    create_verified_backup(source, backup)
    restored = tmp_path / "restored.db"
    evidence = restore_verified_backup(backup, restored, production_path=tmp_path / "prod.db")
    assert evidence.business_digests_match is True
    assert evidence.valid is True
