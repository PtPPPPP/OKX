from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

import app.storage.database_backup as database_backup_module
import app.storage.migration_execution as migration_execution_module
import app.storage.migration_workflow as migration_workflow_module
import app.storage.migrations as migrations_module
from app.cli import app as cli_app
from app.storage.database_backup import DatabaseBackupError, file_identity
from app.storage.migration_execution import MigrationExecutionError
from app.storage.migration_workflow import (
    AuthorizedMigrationOutcome,
    MigrationWorkflowError,
    build_migration_plan,
    execute_authorized_migration,
    load_plan_file,
)
from app.storage.migrations import MIGRATIONS, Migration, MigrationAction, MigrationManager
from tests.migration_fakes import build_synthetic_v21_database

_CONFIRMATION_ID = "approval-CLAUDE-0001"


@pytest.fixture
def protected_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    protected = build_synthetic_v21_database(tmp_path / "protected.db")
    monkeypatch.setattr(database_backup_module, "DEFAULT_PRODUCTION_DATABASE", protected)
    monkeypatch.setattr(migration_execution_module, "DEFAULT_PRODUCTION_DATABASE", protected)
    monkeypatch.setattr(migrations_module, "_PROTECTED_PRODUCTION_DATABASE", protected)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{protected}")
    return protected


def _plan_file_for(database: Path, tmp_path: Path) -> Path:
    report = build_migration_plan(database)
    plan_file = tmp_path / "plan.json"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        json.dumps(report.payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return plan_file


def _execute(
    database: Path, plan_file: Path, confirmation: str = _CONFIRMATION_ID
) -> AuthorizedMigrationOutcome:
    return execute_authorized_migration(
        database_path=database,
        plan=load_plan_file(plan_file),
        operator_confirmation_id=confirmation,
    )


def _audit_records(database: Path) -> list[dict[str, object]]:
    audit = database.parent / "migration_audit.jsonl"
    if not audit.is_file():
        return []
    return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line]


def test_plan_is_read_only_and_binds_database_identity(
    protected_environment: Path, tmp_path: Path
) -> None:
    before = file_identity(protected_environment)
    report = build_migration_plan(protected_environment)

    assert report.current_schema_version == 21
    assert report.target_schema_version == MIGRATIONS[-1].version
    assert report.pending_migrations == (
        MIGRATIONS[21].name,
        MIGRATIONS[22].name,
    )
    assert report.compatibility_status == "compatible"
    assert report.backup_required is True
    assert report.authorization_required is True
    assert not report.blockers
    assert file_identity(protected_environment) == before
    assert MigrationManager(protected_environment).status().current_version == 21

    plan_file = _plan_file_for(protected_environment, tmp_path)
    loaded = load_plan_file(plan_file)
    assert loaded["plan_sha256"] == report.plan_sha256
    assert loaded["database_sha256"] == before.sha256
    assert file_identity(protected_environment) == before


def test_authorized_migration_executes_successfully_with_audit_and_backup(
    protected_environment: Path, tmp_path: Path
) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)

    outcome = _execute(protected_environment, plan_file)

    assert outcome.result == "MIGRATED"
    assert outcome.from_version == 21
    assert outcome.to_version == MIGRATIONS[-1].version
    assert outcome.applied_migrations == (MIGRATIONS[21].name, MIGRATIONS[22].name)
    assert outcome.operator_confirmation_id == _CONFIRMATION_ID
    assert Path(outcome.backup_path).is_file()
    assert MigrationManager(protected_environment).status().current_version == (
        MIGRATIONS[-1].version
    )
    records = _audit_records(protected_environment)
    assert [record["result"] for record in records] == ["started", "success"]
    success = records[-1]
    assert success["operator_confirmation_id"] == _CONFIRMATION_ID
    assert success["from_version"] == 21
    assert success["to_version"] == MIGRATIONS[-1].version
    assert success["plan_sha256"] == outcome.plan_sha256
    assert set(success) <= {
        "recorded_at",
        "database_path",
        "database_sha256",
        "from_version",
        "to_version",
        "plan_sha256",
        "operator_confirmation_id",
        "event",
        "result",
        "applied_migrations",
        "integrity_check",
        "foreign_key_violations",
        "reason_code",
        "reason",
    }


def test_migration_without_authorization_is_blocked(
    protected_environment: Path, tmp_path: Path
) -> None:
    before = file_identity(protected_environment)
    plan_file = _plan_file_for(protected_environment, tmp_path)

    with pytest.raises(MigrationWorkflowError, match="operator-confirmation-id"):
        _execute(protected_environment, plan_file, confirmation="   ")

    assert file_identity(protected_environment) == before
    assert MigrationManager(protected_environment).status().current_version == 21
    assert _audit_records(protected_environment) == []

    result = CliRunner().invoke(
        cli_app,
        ["db-migrate-authorized", "--plan", str(plan_file)],
    )
    assert result.exit_code == 2  # typer rejects the missing required option
    assert file_identity(protected_environment) == before


def test_missing_or_invalid_plan_file_is_rejected_with_guidance(
    protected_environment: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-plan.json"
    with pytest.raises(MigrationWorkflowError, match="db-migrate-plan"):
        load_plan_file(missing)

    result = CliRunner().invoke(
        cli_app,
        [
            "db-migrate-authorized",
            "--plan",
            str(missing),
            "--operator-confirmation-id",
            _CONFIRMATION_ID,
        ],
    )
    assert result.exit_code == 1
    assert "db-migrate-plan" in result.output

    plan_file = _plan_file_for(protected_environment, tmp_path)
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    del payload["plan_sha256"]
    plan_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MigrationWorkflowError, match="plan_sha256"):
        load_plan_file(plan_file)


def test_authorization_database_mismatch_is_blocked(
    protected_environment: Path, tmp_path: Path
) -> None:
    other = build_synthetic_v21_database(tmp_path / "other.db")
    plan_file = _plan_file_for(other, tmp_path)

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "authorization_database_mismatch"
    assert MigrationManager(protected_environment).status().current_version == 21
    assert MigrationManager(other).status().current_version == 21


def test_authorization_from_version_mismatch_is_blocked(
    protected_environment: Path, tmp_path: Path
) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    payload["current_schema_version"] = 20
    plan_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "authorization_from_version_mismatch"
    assert MigrationManager(protected_environment).status().current_version == 21


def test_authorization_target_mismatch_is_blocked(
    protected_environment: Path, tmp_path: Path
) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    payload["target_schema_version"] = 22
    plan_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "authorization_target_mismatch"
    assert MigrationManager(protected_environment).status().current_version == 21


def test_plan_changed_after_authorization_is_blocked(
    protected_environment: Path, tmp_path: Path
) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    payload["plan_sha256"] = "0" * 64
    plan_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "authorization_plan_changed"
    assert MigrationManager(protected_environment).status().current_version == 21


def _unprotect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Temporarily lift the protected-path guard for direct test migration."""
    monkeypatch.setattr(
        migrations_module, "_PROTECTED_PRODUCTION_DATABASE", tmp_path / "not-protected.db"
    )


def _protect(monkeypatch: pytest.MonkeyPatch, protected: Path) -> None:
    monkeypatch.setattr(migrations_module, "_PROTECTED_PRODUCTION_DATABASE", protected)


def test_stale_authorization_after_database_change_is_blocked(
    protected_environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)
    _unprotect(monkeypatch, tmp_path)
    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        migrations_module.MIGRATIONS[:22],
    )
    MigrationManager(protected_environment).migrate(backup=False)
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)
    _protect(monkeypatch, protected_environment)

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "authorization_stale_database_changed"
    assert MigrationManager(protected_environment).status().current_version == 22


def test_noop_migration_succeeds_without_schema_mutation(
    protected_environment: Path, tmp_path: Path
) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)
    _execute(protected_environment, plan_file)
    after_migration = file_identity(protected_environment)

    noop_plan_file = _plan_file_for(protected_environment, tmp_path / "noop-dir")
    outcome = _execute(protected_environment, noop_plan_file)

    assert outcome.result == "NO_OP"
    assert outcome.applied_migrations == ()
    assert file_identity(protected_environment) == after_migration
    assert MigrationManager(protected_environment).status().current_version == (
        MIGRATIONS[-1].version
    )
    results = [record["result"] for record in _audit_records(protected_environment)]
    assert results == ["started", "success", "no_op"]


def test_reused_plan_after_success_is_blocked(protected_environment: Path, tmp_path: Path) -> None:
    plan_file = _plan_file_for(protected_environment, tmp_path)
    _execute(protected_environment, plan_file)
    migrated = file_identity(protected_environment)

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "authorization_stale_database_changed"
    assert file_identity(protected_environment) == migrated
    assert MigrationManager(protected_environment).status().current_version == (
        MIGRATIONS[-1].version
    )


def test_backup_failure_blocks_migration(
    protected_environment: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = file_identity(protected_environment)
    plan_file = _plan_file_for(protected_environment, tmp_path)

    def broken_backup(source: Path, destination: Path) -> None:
        raise DatabaseBackupError("注入的备份失败")

    monkeypatch.setattr(migration_workflow_module, "create_verified_backup", broken_backup)

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "backup_or_restore_failed_migration_blocked"
    assert file_identity(protected_environment) == before
    records = _audit_records(protected_environment)
    assert records[-1]["result"] == "blocked_or_failed"
    assert records[-1]["reason_code"] == "backup_or_restore_failed_migration_blocked"


def test_faulty_migration_is_caught_in_rehearsal_and_blocks_production(
    protected_environment: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = file_identity(protected_environment)
    plan_file = _plan_file_for(protected_environment, tmp_path)
    faulty_last = replace(
        MIGRATIONS[-1],
        apply=_fault_after_real_migration(MIGRATIONS[-1]),
    )
    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        (*MIGRATIONS[:-1], faulty_last),
    )

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code.startswith("rehearsal_")
    assert file_identity(protected_environment) == before
    assert MigrationManager(protected_environment).status().current_version == 21


def test_execution_stage_failure_is_audited_and_blocked(
    protected_environment: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = file_identity(protected_environment)
    plan_file = _plan_file_for(protected_environment, tmp_path)

    def broken_executor(**_kwargs: object) -> None:
        raise MigrationExecutionError("注入的执行失败")

    monkeypatch.setattr(
        migration_workflow_module,
        "execute_authorized_production_migration",
        broken_executor,
    )

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "execution_failed"
    assert file_identity(protected_environment) == before
    assert MigrationManager(protected_environment).status().current_version == 21
    records = _audit_records(protected_environment)
    assert records[-1]["reason_code"] == "execution_failed"


def test_future_schema_is_blocked(
    protected_environment: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unprotect(monkeypatch, tmp_path)
    MigrationManager(protected_environment).migrate(backup=False)
    _protect(monkeypatch, protected_environment)
    with sqlite3.connect(protected_environment) as connection:
        connection.execute(
            """INSERT INTO schema_migrations(version,name,checksum,applied_at,execution_status)
            VALUES (999,'future','fixture','2026-01-01T00:00:00+00:00','successful')"""
        )
        connection.commit()

    plan = build_migration_plan(protected_environment)
    assert plan.compatibility_status == "database_newer_than_application"
    assert plan.blockers == ("database_schema_is_newer_than_application",)
    plan_file = _plan_file_for(protected_environment, tmp_path)

    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "database_newer_than_application"
    with sqlite3.connect(protected_environment) as connection:
        version = connection.execute(
            """SELECT MAX(version) FROM schema_migrations
            WHERE execution_status IN ('successful','adopted')"""
        ).fetchone()[0]
    assert version == 999


def test_corrupt_schema_is_blocked(protected_environment: Path, tmp_path: Path) -> None:
    with protected_environment.open("r+b") as file:
        file.seek(0)
        file.write(b"not-a-sqlite-database-at-all")

    with pytest.raises(MigrationWorkflowError) as exc_info:
        build_migration_plan(protected_environment)
    assert exc_info.value.reason_code == "database_status_unreadable"
    plan_file = _plan_file_for(tmp_path / "unrelated.db", tmp_path)
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    payload["database_path"] = str(protected_environment)
    plan_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code in (
        "database_status_unreadable",
        "authorization_stale_database_changed",
    )


def test_runtime_activity_blocks_authorized_migration(
    protected_environment: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(protected_environment) as connection:
        connection.execute(
            """INSERT INTO continuous_run_locks(lock_name, run_id, host_id, process_id,
            acquired_at, last_renewed_at, lease_expires_at)
            VALUES ('shadow', 'run-1', 'host-1', 1, '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00', '2999-01-01T00:00:00+00:00')"""
        )
        connection.commit()

    plan = build_migration_plan(protected_environment)
    assert plan.runtime_active is True
    assert "runtime_is_active" in plan.blockers
    plan_file = _plan_file_for(protected_environment, tmp_path)
    with pytest.raises(MigrationWorkflowError) as exc_info:
        _execute(protected_environment, plan_file)
    assert exc_info.value.reason_code == "runtime_is_active"
    assert MigrationManager(protected_environment).status().current_version == 21


def test_cli_plan_is_read_only_and_writes_plan_file(
    protected_environment: Path, tmp_path: Path
) -> None:
    before = file_identity(protected_environment)
    output = tmp_path / "plan.json"

    result = CliRunner().invoke(cli_app, ["db-migrate-plan", "--output", str(output)])

    assert result.exit_code == 0
    assert '"plan_file"' in result.output
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["current_schema_version"] == 21
    assert written["target_schema_version"] == MIGRATIONS[-1].version
    assert written["pending_migrations"] == [MIGRATIONS[21].name, MIGRATIONS[22].name]
    assert file_identity(protected_environment) == before

    result_again = CliRunner().invoke(cli_app, ["db-migrate-plan", "--output", str(output)])
    assert result_again.exit_code == 1
    assert "拒绝覆盖" in result_again.output


def test_cli_authorized_migration_end_to_end_and_noop(
    protected_environment: Path, tmp_path: Path
) -> None:
    output = tmp_path / "plan.json"
    assert CliRunner().invoke(cli_app, ["db-migrate-plan", "--output", str(output)]).exit_code == 0

    result = CliRunner().invoke(
        cli_app,
        [
            "db-migrate-authorized",
            "--plan",
            str(output),
            "--operator-confirmation-id",
            _CONFIRMATION_ID,
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["result"] == "MIGRATED"
    assert payload["to_version"] == MIGRATIONS[-1].version
    assert MigrationManager(protected_environment).status().current_version == (
        MIGRATIONS[-1].version
    )

    noop_plan = tmp_path / "noop-plan.json"
    assert (
        CliRunner().invoke(cli_app, ["db-migrate-plan", "--output", str(noop_plan)]).exit_code == 0
    )
    noop = CliRunner().invoke(
        cli_app,
        [
            "db-migrate-authorized",
            "--plan",
            str(noop_plan),
            "--operator-confirmation-id",
            _CONFIRMATION_ID,
        ],
    )
    assert noop.exit_code == 0
    assert json.loads(noop.output)["result"] == "NO_OP"


def test_cli_authorized_blocked_output_names_versions_and_next_step(
    protected_environment: Path, tmp_path: Path
) -> None:
    output = tmp_path / "plan.json"
    assert CliRunner().invoke(cli_app, ["db-migrate-plan", "--output", str(output)]).exit_code == 0
    with protected_environment.open("ab") as file:
        file.write(b"stale")
    result = CliRunner().invoke(
        cli_app,
        [
            "db-migrate-authorized",
            "--plan",
            str(output),
            "--operator-confirmation-id",
            _CONFIRMATION_ID,
        ],
    )
    assert result.exit_code == 1
    assert "authorization_stale_database_changed" in result.output
    assert "db-migrate-plan" in result.output
    assert "21" in result.output


def _fault_after_real_migration(migration: Migration) -> MigrationAction:
    def apply(connection: sqlite3.Connection) -> None:
        migration.apply(connection)
        raise sqlite3.OperationalError("injected migration interruption")

    return apply
