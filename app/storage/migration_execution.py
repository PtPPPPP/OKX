from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.storage.database_backup import (
    DEFAULT_PRODUCTION_DATABASE,
    BackupVerification,
    file_identity,
    verify_backup,
)
from app.storage.migration_gate import (
    MigrationAuthorizationError,
    MigrationExecutionAuthorization,
    MigrationPreflightResult,
)
from app.storage.migrations import MIGRATIONS, MigrationManager


class MigrationExecutionError(RuntimeError):
    pass


_PRODUCTION_PERMIT_SEAL = object()


class _ProductionMigrationPermit:
    __slots__ = ("authorization", "database_path", "seal", "used")

    def __init__(
        self,
        database_path: Path,
        authorization: MigrationExecutionAuthorization,
    ) -> None:
        self.database_path = database_path
        self.authorization = authorization
        self.seal = _PRODUCTION_PERMIT_SEAL
        self.used = False


@dataclass(frozen=True, slots=True)
class MigrationExecutionResult:
    database_path: str
    source_sha256: str
    source_schema_version: int
    target_schema_version: int
    applied_migrations: tuple[str, ...]
    integrity_check: str
    foreign_key_violations: int

    @property
    def valid(self) -> bool:
        return (
            self.target_schema_version == MIGRATIONS[-1].version
            and self.integrity_check == "ok"
            and self.foreign_key_violations == 0
        )


def execute_authorized_temporary_migration(
    *,
    database_path: Path,
    preflight: MigrationPreflightResult,
    authorization: MigrationExecutionAuthorization | None,
) -> MigrationExecutionResult:
    """Run the real migration runner only for an authorized non-production database copy."""
    database_path = database_path.resolve()
    if database_path == DEFAULT_PRODUCTION_DATABASE:
        raise MigrationExecutionError("禁止对正式数据库执行迁移")
    return _execute_authorized_migration(
        database_path=database_path,
        preflight=preflight,
        authorization=authorization,
        production=False,
    )


def execute_authorized_production_migration(
    *,
    database_path: Path,
    backup_path: Path,
    preflight: MigrationPreflightResult,
    authorization: MigrationExecutionAuthorization | None,
) -> MigrationExecutionResult:
    """Run the real runner once on the protected database after fresh backup verification."""
    database_path = database_path.resolve()
    if database_path != DEFAULT_PRODUCTION_DATABASE:
        raise MigrationExecutionError("正式迁移入口只接受固定正式数据库路径")
    if authorization is None:
        raise MigrationAuthorizationError("缺少迁移执行授权")
    identity = file_identity(database_path)
    authorization.validate(preflight, current_source_sha256=identity.sha256)
    backup = verify_backup(backup_path)
    _validate_production_backup(backup, database_path, preflight)
    return _execute_authorized_migration(
        database_path=database_path,
        preflight=preflight,
        authorization=authorization,
        production=True,
    )


def _execute_authorized_migration(
    *,
    database_path: Path,
    preflight: MigrationPreflightResult,
    authorization: MigrationExecutionAuthorization | None,
    production: bool,
) -> MigrationExecutionResult:
    if authorization is None:
        raise MigrationAuthorizationError("缺少迁移执行授权")
    if str(database_path) != preflight.production_path:
        raise MigrationAuthorizationError("迁移目标与 preflight 绑定路径不一致")

    identity = file_identity(database_path)
    authorization.validate(preflight, current_source_sha256=identity.sha256)
    manager = MigrationManager(database_path)
    before = manager.status()
    if not before.compatible:
        raise MigrationExecutionError("迁移目标结构或历史校验和不兼容")
    if before.current_version != authorization.source_schema_version:
        raise MigrationAuthorizationError("迁移目标 schema version 与授权不一致")
    if before.target_version != authorization.target_schema_version:
        raise MigrationAuthorizationError("migration runner 目标版本与授权不一致")

    authorization.consume(preflight, current_source_sha256=identity.sha256)
    permit = _ProductionMigrationPermit(database_path, authorization) if production else None
    applied = manager.migrate(
        backup=False,
        production_permit=permit,
        target_version=authorization.target_schema_version,
    )
    after = manager.status()
    if after.current_version != authorization.target_schema_version or after.pending:
        raise MigrationExecutionError("授权迁移未达到目标 schema version")
    integrity_check, foreign_key_violations = _database_integrity(database_path)
    result = MigrationExecutionResult(
        str(database_path),
        identity.sha256,
        before.current_version,
        after.current_version,
        applied,
        integrity_check,
        foreign_key_violations,
    )
    if not result.valid:
        raise MigrationExecutionError("迁移后数据库完整性校验失败")
    return result


def _validate_production_backup(
    backup: BackupVerification,
    database_path: Path,
    preflight: MigrationPreflightResult,
) -> None:
    manifest = backup.manifest
    if (
        not backup.valid
        or Path(manifest.source_path).resolve() != database_path
        or manifest.source_sha256 != preflight.source_sha256
        or manifest.source_schema_version != preflight.source_schema_version
        or manifest.backup_id != preflight.backup_id
        or manifest.backup_sha256 != preflight.backup_sha256
    ):
        raise MigrationAuthorizationError("已验证 backup 与正式迁移授权证据不匹配")


def _consume_production_migration_permit(permit: object, database_path: Path) -> None:
    """Called by MigrationManager immediately before opening a protected write transaction."""
    if (
        not isinstance(permit, _ProductionMigrationPermit)
        or permit.seal is not _PRODUCTION_PERMIT_SEAL
        or permit.used
        or permit.database_path != database_path
        or not permit.authorization.issued_by_gate
        or not permit.authorization.consumed
    ):
        raise MigrationAuthorizationError("正式 migration runner 缺少有效的一次性执行许可")
    permit.used = True


def _database_integrity(path: Path) -> tuple[str, int]:
    connection = sqlite3.connect(path)
    try:
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        return integrity_check, foreign_key_violations
    except sqlite3.Error as exc:
        raise MigrationExecutionError(f"迁移后 SQLite 校验失败: {exc}") from exc
    finally:
        connection.close()
