from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.storage.database_backup import (
    BackupVerification,
    RestoreEvidence,
    create_verified_backup,
    file_identity,
    restore_evidence,
    restore_verified_backup,
    verify_backup,
)
from app.storage.migration_gate import (
    MigrationAuthorizationError,
    MigrationExecutionAuthorization,
    MigrationPreflightResult,
    MigrationRehearsalEvidence,
    PostMigrationVerification,
    evaluate_database_migration_preflight,
    issue_migration_execution_authorization,
    rollback_decision,
)
from tests.migration_fakes import build_synthetic_v21_database


@pytest.fixture
def v21_database(tmp_path: Path) -> Path:
    return build_synthetic_v21_database(tmp_path / "synthetic-v21.db")


@pytest.fixture
def gate_inputs(
    v21_database: Path,
) -> tuple[BackupVerification, RestoreEvidence, MigrationRehearsalEvidence]:
    backup_path = tmp_backup_path(v21_database)
    create_verified_backup(v21_database, backup_path)
    backup = verify_backup(backup_path)
    restored = v21_database.parent / "restored.db"
    restore_verified_backup(backup_path, restored, production_path=v21_database)
    restore = restore_evidence(backup_path, restored)
    rehearsal = MigrationRehearsalEvidence(
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
    return backup, restore, rehearsal


def tmp_backup_path(database: Path) -> Path:
    return database.parent / f"{database.name}.verified.db"


def _preflight(
    v21_database: Path,
    backup: BackupVerification | None,
    restore: RestoreEvidence | None,
    rehearsal: MigrationRehearsalEvidence | None,
    *,
    expected_sha256: str | None = None,
    expected_source_version: int = 21,
    expected_target_version: int = 23,
) -> MigrationPreflightResult:
    return evaluate_database_migration_preflight(
        production_path=v21_database,
        expected_production_path=v21_database,
        expected_source_sha256=expected_sha256 or file_identity(v21_database).sha256,
        expected_source_schema_version=expected_source_version,
        expected_target_schema_version=expected_target_version,
        backup=backup,
        restore=restore,
        rehearsal=rehearsal,
        bounded_demo_running=False,
        continuous_shadow_running=False,
    )


def test_valid_preflight_is_ready_and_authorization_is_bound_and_one_use(
    v21_database: Path,
    gate_inputs: tuple[BackupVerification, RestoreEvidence, MigrationRehearsalEvidence],
) -> None:
    initial_backup, initial_restore, initial_rehearsal = gate_inputs
    backup: BackupVerification | None = initial_backup
    restore: RestoreEvidence | None = initial_restore
    rehearsal: MigrationRehearsalEvidence | None = initial_rehearsal
    result = _preflight(v21_database, backup, restore, rehearsal)

    assert result.ready
    authorization = issue_migration_execution_authorization(
        result,
        operator_confirmation_id="separate-future-production-approval-required",
    )
    authorization.consume_for_validation_only(result, current_source_sha256=result.source_sha256)
    with pytest.raises(MigrationAuthorizationError, match="已被使用"):
        authorization.validate(result, current_source_sha256=result.source_sha256)


@pytest.mark.parametrize(
    ("change", "expected_blocker"),
    [
        ("source_hash", "production_sha256_mismatch"),
        ("source_version", "backup_is_not_bound_to_current_source"),
        ("backup_missing", "verified_backup_is_missing_or_invalid"),
        ("backup_hash", "verified_backup_is_missing_or_invalid"),
        ("backup_manifest", "verified_backup_is_missing_or_invalid"),
        ("backup_integrity", "verified_backup_is_missing_or_invalid"),
        ("restore_missing", "restore_rehearsal_is_missing_or_invalid"),
        ("rehearsal_missing", "migration_rehearsal_is_missing_or_invalid"),
        ("target_version", "target_schema_version_is_not_current"),
    ],
)
def test_preflight_fail_closed_matrix(
    change: str,
    expected_blocker: str,
    v21_database: Path,
    gate_inputs: tuple[BackupVerification, RestoreEvidence, MigrationRehearsalEvidence],
) -> None:
    initial_backup, initial_restore, initial_rehearsal = gate_inputs
    backup: BackupVerification | None = initial_backup
    restore: RestoreEvidence | None = initial_restore
    rehearsal: MigrationRehearsalEvidence | None = initial_rehearsal
    expected_sha256: str | None = None
    expected_source_version = 21
    expected_target_version = 23
    if change == "source_hash":
        expected_sha256 = "changed"
    elif change == "source_version":
        expected_source_version = 22
    elif change == "backup_missing":
        backup = None
    elif change == "backup_hash":
        backup = replace(
            initial_backup,
            manifest=replace(initial_backup.manifest, backup_sha256="changed"),
        )
    elif change == "backup_manifest":
        backup = replace(initial_backup, manifest=replace(initial_backup.manifest, backup_id=""))
    elif change == "backup_integrity":
        backup = replace(
            initial_backup,
            manifest=replace(initial_backup.manifest, integrity_check="bad"),
        )
    elif change == "restore_missing":
        restore = None
    elif change == "rehearsal_missing":
        rehearsal = None
    elif change == "target_version":
        expected_target_version = 22
    else:
        raise AssertionError(f"unknown gate matrix change: {change}")

    result = _preflight(
        v21_database,
        backup,
        restore,
        rehearsal,
        expected_sha256=expected_sha256,
        expected_source_version=expected_source_version,
        expected_target_version=expected_target_version,
    )

    assert not result.ready
    assert expected_blocker in result.blockers


def test_stale_authorization_is_rejected_after_source_identity_changes(
    v21_database: Path,
    gate_inputs: tuple[BackupVerification, RestoreEvidence, MigrationRehearsalEvidence],
) -> None:
    backup, restore, rehearsal = gate_inputs
    result = _preflight(v21_database, backup, restore, rehearsal)
    authorization = issue_migration_execution_authorization(
        result,
        operator_confirmation_id="separate-future-production-approval-required",
    )

    with pytest.raises(MigrationAuthorizationError, match="不匹配"):
        authorization.validate(result, current_source_sha256="changed")


def test_directly_constructed_authorization_and_preflight_cannot_bypass_gate(
    v21_database: Path,
    gate_inputs: tuple[BackupVerification, RestoreEvidence, MigrationRehearsalEvidence],
) -> None:
    backup, restore, rehearsal = gate_inputs
    result = _preflight(v21_database, backup, restore, rehearsal)
    forged_authorization = MigrationExecutionAuthorization(
        result.production_path,
        result.source_sha256,
        result.source_schema_version,
        result.target_schema_version,
        result.backup_id,
        result.backup_sha256,
        "forged",
    )
    with pytest.raises(MigrationAuthorizationError, match="不是由授权门签发"):
        forged_authorization.validate(result, current_source_sha256=result.source_sha256)

    forged_preflight = replace(result)
    with pytest.raises(MigrationAuthorizationError, match="不是由预检门签发"):
        issue_migration_execution_authorization(
            forged_preflight,
            operator_confirmation_id="forged",
        )


def test_preflight_binds_to_expected_production_path(
    v21_database: Path,
    gate_inputs: tuple[BackupVerification, RestoreEvidence, MigrationRehearsalEvidence],
    tmp_path: Path,
) -> None:
    backup, restore, rehearsal = gate_inputs
    result = evaluate_database_migration_preflight(
        production_path=v21_database,
        expected_production_path=tmp_path / "other-production.db",
        expected_source_sha256=file_identity(v21_database).sha256,
        expected_source_schema_version=21,
        expected_target_schema_version=23,
        backup=backup,
        restore=restore,
        rehearsal=rehearsal,
        bounded_demo_running=False,
        continuous_shadow_running=False,
    )

    assert not result.ready
    assert "production_path_is_not_expected" in result.blockers


def test_post_migration_verification_requires_rollback_on_any_failed_condition() -> None:
    assert (
        rollback_decision(PostMigrationVerification(23, True, True, True)) == "NO_ROLLBACK_REQUIRED"
    )
    assert (
        rollback_decision(PostMigrationVerification(23, True, False, True)) == "ROLLBACK_REQUIRED"
    )
    assert rollback_decision(PostMigrationVerification(22, True, True, True)) == "ROLLBACK_REQUIRED"
