from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import app.storage.migration_execution as migration_execution_module
import app.storage.migrations as migrations_module
from app.storage.database import Database
from app.storage.database_backup import (
    BackupVerification,
    RestoreEvidence,
    create_verified_backup,
    file_identity,
    restore_evidence,
    restore_verified_backup,
    verify_backup,
)
from app.storage.migration_execution import (
    MigrationExecutionError,
    execute_authorized_production_migration,
    execute_authorized_temporary_migration,
)
from app.storage.migration_gate import (
    MigrationAuthorizationError,
    MigrationExecutionAuthorization,
    MigrationPreflightResult,
    MigrationRehearsalEvidence,
    evaluate_database_migration_preflight,
    issue_migration_execution_authorization,
)
from app.storage.migrations import MIGRATIONS, MigrationError, MigrationManager
from app.storage.repositories import TradingRepository
from tests.migration_fakes import build_synthetic_v21_database


@pytest.fixture
def v21_database(tmp_path: Path) -> Path:
    return build_synthetic_v21_database(tmp_path / "synthetic-v21.db")


def _rehearsal(v21_database: Path) -> MigrationRehearsalEvidence:
    return MigrationRehearsalEvidence(
        file_identity(v21_database).sha256,
        21,
        23,
        True,
        True,
        True,
        True,
        True,
        True,
    )


def _execution_evidence(
    tmp_path: Path,
    v21_database: Path,
) -> tuple[Path, BackupVerification, RestoreEvidence, MigrationPreflightResult]:
    backup_path = tmp_path / "verified-v21.db"
    create_verified_backup(v21_database, backup_path)
    backup = verify_backup(backup_path)
    working = tmp_path / "authorized-working-v21.db"
    restore_verified_backup(backup_path, working, production_path=v21_database)
    restore = restore_evidence(backup_path, working)
    preflight = evaluate_database_migration_preflight(
        production_path=working,
        expected_production_path=working,
        expected_source_sha256=file_identity(v21_database).sha256,
        expected_source_schema_version=21,
        expected_target_schema_version=23,
        backup=backup,
        restore=restore,
        rehearsal=_rehearsal(v21_database),
        bounded_demo_running=False,
        continuous_shadow_running=False,
    )
    assert preflight.ready
    return working, backup, restore, preflight


def _authorization(preflight: MigrationPreflightResult) -> MigrationExecutionAuthorization:
    return issue_migration_execution_authorization(
        preflight,
        operator_confirmation_id="temporary-execution-rehearsal-only",
    )


def test_authorized_temporary_copy_executes_real_runner_end_to_end(
    tmp_path: Path, v21_database: Path
) -> None:
    working, _, _, preflight = _execution_evidence(tmp_path, v21_database)
    authorization = _authorization(preflight)

    result = execute_authorized_temporary_migration(
        database_path=working,
        preflight=preflight,
        authorization=authorization,
    )

    assert result.valid
    assert result.source_schema_version == 21
    assert result.target_schema_version == 23
    assert result.applied_migrations == tuple(migration.name for migration in MIGRATIONS[21:])
    assert MigrationManager(working).status().current_version == 23
    database = Database(f"sqlite:///{working}")
    database.initialize()
    assert TradingRepository(database).private_state_snapshot().ws_watermark == 0


def test_execution_rejects_missing_or_blocked_authorization_without_migrating(
    tmp_path: Path, v21_database: Path
) -> None:
    working, _, _, preflight = _execution_evidence(tmp_path, v21_database)

    with pytest.raises(MigrationAuthorizationError, match="缺少"):
        execute_authorized_temporary_migration(
            database_path=working,
            preflight=preflight,
            authorization=None,
        )
    assert MigrationManager(working).status().current_version == 21

    blocked = replace(preflight, blockers=("injected_blocker",))
    with pytest.raises(MigrationAuthorizationError, match="不是由预检门签发"):
        execute_authorized_temporary_migration(
            database_path=working,
            preflight=blocked,
            authorization=_authorization(preflight),
        )
    assert MigrationManager(working).status().current_version == 21


def test_execution_rejects_path_hash_version_and_backup_binding_bypasses(
    tmp_path: Path, v21_database: Path
) -> None:
    working, _, _, preflight = _execution_evidence(tmp_path, v21_database)
    other = tmp_path / "other.db"
    shutil.copyfile(working, other)
    with pytest.raises(MigrationAuthorizationError, match="绑定路径"):
        execute_authorized_temporary_migration(
            database_path=other,
            preflight=preflight,
            authorization=_authorization(preflight),
        )

    stale = _authorization(preflight)
    with working.open("ab") as file:
        file.write(b"stale")
    with pytest.raises(MigrationAuthorizationError, match="证据不匹配"):
        execute_authorized_temporary_migration(
            database_path=working,
            preflight=preflight,
            authorization=stale,
        )

    clean_database = build_synthetic_v21_database(tmp_path / "clean" / "synthetic-v21.db")
    clean, _, _, clean_preflight = _execution_evidence(tmp_path / "clean", clean_database)
    wrong_backup = replace(_authorization(clean_preflight), backup_sha256="changed")
    with pytest.raises(MigrationAuthorizationError, match="不是由授权门签发"):
        execute_authorized_temporary_migration(
            database_path=clean,
            preflight=clean_preflight,
            authorization=wrong_backup,
        )
    assert MigrationManager(clean).status().current_version == 21


def test_execution_consumes_authorization_before_runner_and_rejects_reuse(
    tmp_path: Path, v21_database: Path
) -> None:
    working, _, _, preflight = _execution_evidence(tmp_path, v21_database)
    authorization = _authorization(preflight)
    execute_authorized_temporary_migration(
        database_path=working,
        preflight=preflight,
        authorization=authorization,
    )

    with pytest.raises(MigrationAuthorizationError, match="已被使用"):
        execute_authorized_temporary_migration(
            database_path=working,
            preflight=preflight,
            authorization=authorization,
        )
    assert MigrationManager(working).status().current_version == 23


def test_execution_hard_rejects_production_path_before_runner(
    tmp_path: Path,
    v21_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / "protected.db"
    shutil.copyfile(v21_database, protected)
    monkeypatch.setattr(migration_execution_module, "DEFAULT_PRODUCTION_DATABASE", protected)
    _, _, _, preflight = _execution_evidence(tmp_path, v21_database)
    authorization = _authorization(preflight)

    with pytest.raises(MigrationExecutionError, match="禁止对正式数据库"):
        execute_authorized_temporary_migration(
            database_path=protected,
            preflight=preflight,
            authorization=authorization,
        )
    assert not authorization.consumed
    assert MigrationManager(protected).status().current_version == 21


def test_direct_runner_cannot_bypass_authorization_for_production_database(
    tmp_path: Path,
    v21_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = build_synthetic_v21_database(tmp_path / "protected-v21.db")
    monkeypatch.setattr(migrations_module, "_PROTECTED_PRODUCTION_DATABASE", protected)
    before = file_identity(protected)

    with pytest.raises(MigrationError, match="一次性授权执行入口"):
        MigrationManager(protected).migrate(backup=False)

    assert file_identity(protected) == before
    assert MigrationManager(protected).status().current_version == 21


def test_authorized_production_entry_executes_only_with_verified_backup_and_permit(
    tmp_path: Path,
    v21_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / "protected.db"
    shutil.copyfile(v21_database, protected)
    backup_path = tmp_path / "protected-backup.db"
    create_verified_backup(protected, backup_path)
    backup = verify_backup(backup_path)
    restored = tmp_path / "restore-check.db"
    restore_verified_backup(backup_path, restored, production_path=protected)
    restore = restore_evidence(backup_path, restored)
    preflight = evaluate_database_migration_preflight(
        production_path=protected,
        expected_production_path=protected,
        expected_source_sha256=file_identity(protected).sha256,
        expected_source_schema_version=21,
        expected_target_schema_version=23,
        backup=backup,
        restore=restore,
        rehearsal=_rehearsal(protected),
        bounded_demo_running=False,
        continuous_shadow_running=False,
    )
    authorization = _authorization(preflight)
    monkeypatch.setattr(migration_execution_module, "DEFAULT_PRODUCTION_DATABASE", protected)
    monkeypatch.setattr(migrations_module, "_PROTECTED_PRODUCTION_DATABASE", protected)

    result = execute_authorized_production_migration(
        database_path=protected,
        backup_path=backup_path,
        preflight=preflight,
        authorization=authorization,
    )

    assert result.valid
    assert result.source_schema_version == 21
    assert result.target_schema_version == 23
    assert authorization.consumed
    assert MigrationManager(protected).status().current_version == 23
