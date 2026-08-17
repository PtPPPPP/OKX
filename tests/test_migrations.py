from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import app.storage.migrations as migrations_module
from app.cli import app
from app.storage.database import Database, StorageError
from app.storage.migrations import MIGRATIONS, Migration, MigrationError, MigrationManager


def test_new_database_reaches_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    Database(f"sqlite:///{path}").initialize()

    status = MigrationManager(path).status()
    assert status.current_version == MIGRATIONS[-1].version
    assert status.pending == ()
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "schema_migrations",
        "runs",
        "processed_events",
        "private_state_snapshots",
        "demo_order_proposals",
        "demo_order_proposal_events",
    }.issubset(tables)


def test_legacy_database_requires_explicit_migration_and_is_backed_up(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    seed = Database(f"sqlite:///{path}")
    seed.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("CREATE TABLE account_snapshots(id INTEGER PRIMARY KEY, payload TEXT)")
        connection.execute("INSERT INTO account_snapshots(payload) VALUES ('kept')")

    with pytest.raises(StorageError, match="db-migrate"):
        Database(f"sqlite:///{path}").initialize()

    applied = MigrationManager(path).migrate(backup=True)
    assert "unified_engine_v0_2" in applied
    assert "demo_order_closure_v0_3" in applied
    backups = list((tmp_path / "backups").glob("legacy-*.db"))
    assert backups
    with sqlite3.connect(backups[0]) as backup_connection:
        assert backup_connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with sqlite3.connect(path) as connection:
        value = connection.execute("SELECT payload FROM account_snapshots").fetchone()
        marker = connection.execute(
            "SELECT reason FROM legacy_tables WHERE table_name = 'account_snapshots'"
        ).fetchone()
    assert value == ("kept",)
    assert marker is not None


def test_dry_run_does_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "dry.db"
    plan = MigrationManager(path).migrate(dry_run=True)
    assert plan == tuple(migration.name for migration in MIGRATIONS)
    assert not path.exists()


def test_failed_migration_rolls_back_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed.db"

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE must_rollback(id INTEGER PRIMARY KEY)")
        raise sqlite3.OperationalError("injected failure")

    broken = Migration(1, "broken", "fixture", fail_after_write)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (broken,))

    with pytest.raises(MigrationError, match="事务已回滚"):
        MigrationManager(path).migrate(backup=False)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        failed = connection.execute(
            """SELECT execution_status FROM schema_migrations
            WHERE name = 'broken' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    assert "must_rollback" not in tables
    assert failed == ("failed",)


def test_explicit_target_is_required_for_progress_and_downgrade_is_blocked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target.db"
    manager = MigrationManager(path)

    applied = manager.migrate(backup=False, target_version=5)

    assert applied == tuple(migration.name for migration in MIGRATIONS[:5])
    assert manager.status().current_version == 5
    assert manager.migrate(backup=False, target_version=5) == ()
    with pytest.raises(MigrationError, match="downgrade"):
        manager.migrate(backup=False, target_version=4)
    with pytest.raises(MigrationError, match=r"unknown.*target"):
        manager.migrate(backup=False, target_version=999)


def test_gap_duplicate_and_unknown_schema_are_blocked(tmp_path: Path) -> None:
    gap = tmp_path / "gap.db"
    Database(f"sqlite:///{gap}").initialize()
    with sqlite3.connect(gap) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=2")
    assert not MigrationManager(gap).status().compatible
    with pytest.raises(MigrationError, match="不兼容"):
        MigrationManager(gap).migrate(backup=False)

    duplicate = tmp_path / "duplicate.db"
    Database(f"sqlite:///{duplicate}").initialize()
    with sqlite3.connect(duplicate) as connection:
        row = connection.execute(
            "SELECT version,name,checksum,applied_at,execution_status "
            "FROM schema_migrations WHERE version=1"
        ).fetchone()
        assert row is not None
        connection.execute(
            """INSERT INTO schema_migrations
            (version,name,checksum,applied_at,execution_status) VALUES (?,?,?,?,?)""",
            row,
        )
    assert not MigrationManager(duplicate).status().compatible

    unknown = tmp_path / "unknown.db"
    with sqlite3.connect(unknown) as connection:
        connection.execute("CREATE TABLE unexplained(value TEXT)")
    assert not MigrationManager(unknown).status().compatible


def test_database_lock_and_non_sqlite_migration_failure_are_nonzero_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "locked.db"
    MigrationManager(locked).migrate(backup=False, target_version=5)
    lock = sqlite3.connect(locked)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(MigrationError, match="locked"):
            MigrationManager(locked).migrate(backup=False, target_version=6)
    finally:
        lock.rollback()
        lock.close()

    failed = tmp_path / "runtime-failure.db"

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE must_rollback_runtime(id INTEGER PRIMARY KEY)")
        raise RuntimeError("injected runtime failure")

    broken = Migration(1, "runtime-broken", "fixture", fail_after_write)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (broken,))
    with pytest.raises(MigrationError, match="injected runtime failure"):
        MigrationManager(failed).migrate(backup=False, target_version=1)
    with sqlite3.connect(failed) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='must_rollback_runtime'"
            ).fetchone()
            is None
        )


def test_db_migrate_cli_reports_invalid_target_with_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")

    result = CliRunner().invoke(app, ["db-migrate", "--target-version", "999"])

    assert result.exit_code == 1
    assert "unknown database migration target version" in result.output


def test_noop_migrate_on_protected_path_does_not_require_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prod.db"
    Database(f"sqlite:///{path}").initialize()
    monkeypatch.setattr(migrations_module, "_PROTECTED_PRODUCTION_DATABASE", path.resolve())

    applied = MigrationManager(path).migrate(target_version=MIGRATIONS[-1].version)

    assert applied == ()


def test_migrate_on_protected_path_with_pending_requires_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prod.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations("
            "id INTEGER PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL,"
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL,"
            "execution_status TEXT NOT NULL)"
        )
    monkeypatch.setattr(migrations_module, "_PROTECTED_PRODUCTION_DATABASE", path.resolve())

    with pytest.raises(MigrationError, match="授权"):
        MigrationManager(path).migrate(target_version=MIGRATIONS[-1].version)
