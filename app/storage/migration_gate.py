from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.storage.database_backup import (
    BackupVerification,
    DatabaseBackupError,
    RestoreEvidence,
    file_identity,
)
from app.storage.migrations import MIGRATIONS

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version

_PREFLIGHT_SEAL = object()
_AUTHORIZATION_SEAL = object()


@dataclass(frozen=True, slots=True)
class MigrationRehearsalEvidence:
    source_sha256: str
    source_schema_version: int
    target_schema_version: int
    rehearsal_passed: bool
    schema_integrity_passed: bool
    data_integrity_passed: bool
    idempotency_passed: bool
    failure_recovery_passed: bool
    os_crash_recovery_passed: bool

    @property
    def valid(self) -> bool:
        return (
            self.rehearsal_passed
            and self.schema_integrity_passed
            and self.data_integrity_passed
            and self.idempotency_passed
            and self.failure_recovery_passed
            and self.os_crash_recovery_passed
        )


@dataclass(frozen=True, slots=True)
class MigrationPreflightResult:
    production_path: str
    source_sha256: str
    source_schema_version: int
    target_schema_version: int
    backup_id: str
    backup_sha256: str
    blockers: tuple[str, ...]
    _issuer: object | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def status(self) -> str:
        return "READY" if self.ready else "BLOCKED"

    @property
    def issued_by_gate(self) -> bool:
        return self._issuer is _PREFLIGHT_SEAL


class MigrationAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class PostMigrationVerification:
    target_schema_version: int
    schema_integrity_passed: bool
    data_integrity_passed: bool
    application_compatibility_passed: bool

    @property
    def valid(self) -> bool:
        return (
            self.target_schema_version == CURRENT_SCHEMA_VERSION
            and self.schema_integrity_passed
            and self.data_integrity_passed
            and self.application_compatibility_passed
        )


def rollback_decision(verification: PostMigrationVerification) -> str:
    """Return the only safe rollback decision; this function never restores a database."""
    return "NO_ROLLBACK_REQUIRED" if verification.valid else "ROLLBACK_REQUIRED"


@dataclass(slots=True)
class MigrationExecutionAuthorization:
    database_path: str
    source_sha256: str
    source_schema_version: int
    target_schema_version: int
    backup_id: str
    backup_sha256: str
    operator_confirmation_id: str
    _consumed: bool = field(default=False, init=False, repr=False)
    _issuer: object | None = field(default=None, init=False, repr=False)

    @property
    def consumed(self) -> bool:
        return self._consumed

    @property
    def issued_by_gate(self) -> bool:
        return self._issuer is _AUTHORIZATION_SEAL

    def validate(self, preflight: MigrationPreflightResult, *, current_source_sha256: str) -> None:
        if self._issuer is not _AUTHORIZATION_SEAL:
            raise MigrationAuthorizationError("迁移授权不是由授权门签发")
        if not preflight.issued_by_gate:
            raise MigrationAuthorizationError("迁移预检不是由预检门签发")
        if self._consumed:
            raise MigrationAuthorizationError("迁移授权已被使用")
        if not self.operator_confirmation_id:
            raise MigrationAuthorizationError("缺少明确的操作确认标识")
        if not preflight.ready:
            raise MigrationAuthorizationError("迁移预检未通过")
        if (
            self.database_path != preflight.production_path
            or self.source_sha256 != current_source_sha256
            or self.source_sha256 != preflight.source_sha256
            or self.source_schema_version != preflight.source_schema_version
            or self.target_schema_version != preflight.target_schema_version
            or self.backup_id != preflight.backup_id
            or self.backup_sha256 != preflight.backup_sha256
        ):
            raise MigrationAuthorizationError("迁移授权与当前数据库证据不匹配")

    def consume_for_validation_only(
        self, preflight: MigrationPreflightResult, *, current_source_sha256: str
    ) -> None:
        """Exercise one-use authorization semantics without calling a migration runner."""
        self.consume(preflight, current_source_sha256=current_source_sha256)

    def consume(self, preflight: MigrationPreflightResult, *, current_source_sha256: str) -> None:
        """Consume this authorization exactly once after all bound evidence is validated."""
        self.validate(preflight, current_source_sha256=current_source_sha256)
        self._consumed = True


def issue_migration_execution_authorization(
    preflight: MigrationPreflightResult,
    *,
    operator_confirmation_id: str,
) -> MigrationExecutionAuthorization:
    """Issue a one-use capability only for an authentic, successful preflight."""
    if not preflight.issued_by_gate:
        raise MigrationAuthorizationError("迁移预检不是由预检门签发")
    if not preflight.ready:
        raise MigrationAuthorizationError("迁移预检未通过")
    if not operator_confirmation_id:
        raise MigrationAuthorizationError("缺少明确的操作确认标识")
    authorization = MigrationExecutionAuthorization(
        preflight.production_path,
        preflight.source_sha256,
        preflight.source_schema_version,
        preflight.target_schema_version,
        preflight.backup_id,
        preflight.backup_sha256,
        operator_confirmation_id,
    )
    authorization._issuer = _AUTHORIZATION_SEAL
    return authorization


def evaluate_database_migration_preflight(
    *,
    production_path: Path,
    expected_production_path: Path,
    expected_source_sha256: str,
    expected_source_schema_version: int,
    expected_target_schema_version: int,
    backup: BackupVerification | None,
    restore: RestoreEvidence | None,
    rehearsal: MigrationRehearsalEvidence | None,
    bounded_demo_running: bool,
    continuous_shadow_running: bool,
) -> MigrationPreflightResult:
    """Pure verification gate. It hashes production bytes but never opens it through SQLite."""
    production_path = production_path.resolve()
    expected_production_path = expected_production_path.resolve()
    blockers: list[str] = []
    if production_path != expected_production_path:
        blockers.append("production_path_is_not_expected")
    try:
        identity = file_identity(production_path)
    except DatabaseBackupError:
        identity = None
        blockers.append("production_database_is_missing")
    if identity is not None and identity.sha256 != expected_source_sha256:
        blockers.append("production_sha256_mismatch")
    if backup is None or not backup.valid:
        blockers.append("verified_backup_is_missing_or_invalid")
    elif identity is not None and (
        backup.manifest.source_sha256 != identity.sha256
        or backup.manifest.source_schema_version != expected_source_schema_version
    ):
        blockers.append("backup_is_not_bound_to_current_source")
    if restore is None or not restore.valid:
        blockers.append("restore_rehearsal_is_missing_or_invalid")
    elif identity is not None and restore.source_sha256 != identity.sha256:
        blockers.append("restore_is_not_bound_to_current_source")
    if rehearsal is None or not rehearsal.valid:
        blockers.append("migration_rehearsal_is_missing_or_invalid")
    elif identity is not None and (
        rehearsal.source_sha256 != identity.sha256
        or rehearsal.source_schema_version != expected_source_schema_version
        or rehearsal.target_schema_version != expected_target_schema_version
    ):
        blockers.append("migration_rehearsal_is_not_bound_to_current_source")
    if expected_target_schema_version != CURRENT_SCHEMA_VERSION:
        blockers.append("target_schema_version_is_not_current")
    if bounded_demo_running:
        blockers.append("bounded_demo_is_running")
    if continuous_shadow_running:
        blockers.append("continuous_shadow_is_running")
    result = MigrationPreflightResult(
        str(production_path),
        identity.sha256 if identity is not None else "",
        expected_source_schema_version,
        expected_target_schema_version,
        backup.manifest.backup_id if backup is not None and backup.valid else "",
        backup.manifest.backup_sha256 if backup is not None and backup.valid else "",
        tuple(blockers),
    )
    object.__setattr__(result, "_issuer", _PREFLIGHT_SEAL)
    return result
