from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.storage.migrations import MigrationError, MigrationManager

MANIFEST_VERSION = 1
TOOL_VERSION = "okx-database-backup-v1"
DEFAULT_PRODUCTION_DATABASE = (
    Path(__file__).resolve().parents[2] / "data" / "trading.db"
).resolve()
CRITICAL_BUSINESS_TABLES = (
    "signals",
    "demo_order_proposals",
    "orders",
    "continuous_demo_runs",
    "private_state_snapshots",
)


class DatabaseBackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DatabaseFileIdentity:
    path: str
    size: int
    mtime: str
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_id: str
    source_path: str
    source_schema_version: int
    source_size: int
    source_sha256: str
    source_mtime: str
    backup_path: str
    backup_size: int
    backup_sha256: str
    integrity_check: str
    foreign_key_violations: int
    created_at: str
    manifest_version: int = MANIFEST_VERSION
    tool_version: str = TOOL_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, payload: str) -> BackupManifest:
        try:
            decoded = json.loads(payload)
            return cls(**decoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DatabaseBackupError("备份 manifest 格式无效") from exc


@dataclass(frozen=True, slots=True)
class BackupVerification:
    manifest: BackupManifest
    backup_identity: DatabaseFileIdentity
    schema_version: int
    integrity_check: str
    foreign_key_violations: int

    @property
    def valid(self) -> bool:
        return (
            bool(self.manifest.backup_id)
            and bool(self.manifest.source_path)
            and self.manifest.manifest_version == MANIFEST_VERSION
            and self.manifest.tool_version == TOOL_VERSION
            and self.manifest.backup_path == self.backup_identity.path
            and self.manifest.backup_size == self.backup_identity.size
            and self.manifest.backup_sha256 == self.backup_identity.sha256
            and self.manifest.source_size == self.backup_identity.size
            and self.manifest.source_sha256 == self.backup_identity.sha256
            and self.manifest.source_schema_version == self.schema_version
            and self.manifest.integrity_check == "ok"
            and self.manifest.foreign_key_violations == 0
            and self.integrity_check == "ok"
            and self.foreign_key_violations == 0
        )


@dataclass(frozen=True, slots=True)
class BusinessDataDigest:
    table: str
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RestoreEvidence:
    source_sha256: str
    restored_sha256: str
    source_schema_version: int
    restored_schema_version: int
    integrity_check: str
    foreign_key_violations: int
    business_digests_match: bool

    @property
    def valid(self) -> bool:
        return (
            self.source_sha256 == self.restored_sha256
            and self.source_schema_version == self.restored_schema_version
            and self.integrity_check == "ok"
            and self.foreign_key_violations == 0
            and self.business_digests_match
        )


def create_verified_backup(source: Path, destination: Path) -> BackupManifest:
    """Publish a verified byte-for-byte backup without opening the source through SQLite."""
    source = source.resolve()
    destination = destination.resolve()
    _validate_backup_paths(source, destination)
    _reject_uncheckpointed_wal(source)
    source_before = file_identity(source)
    partial = _partial_path(destination)
    manifest_path = backup_manifest_path(destination)
    partial_manifest = _partial_path(manifest_path)
    try:
        shutil.copyfile(source, partial)
        source_after = file_identity(source)
        backup_identity = file_identity(partial)
        if source_before != source_after:
            raise DatabaseBackupError("备份期间源数据库发生变化，拒绝发布备份")
        if (
            source_before.size != backup_identity.size
            or source_before.sha256 != backup_identity.sha256
        ):
            raise DatabaseBackupError("备份字节校验失败，拒绝发布备份")
        schema_version, integrity_check, foreign_key_violations = _database_checks(partial)
        if integrity_check != "ok" or foreign_key_violations != 0:
            raise DatabaseBackupError("备份 SQLite 完整性校验失败，拒绝发布备份")
        manifest = BackupManifest(
            backup_id=uuid4().hex,
            source_path=str(source),
            source_schema_version=schema_version,
            source_size=source_before.size,
            source_sha256=source_before.sha256,
            source_mtime=source_before.mtime,
            backup_path=str(destination),
            backup_size=backup_identity.size,
            backup_sha256=backup_identity.sha256,
            integrity_check=integrity_check,
            foreign_key_violations=foreign_key_violations,
            created_at=datetime.now(UTC).isoformat(),
        )
        _write_new_file(partial_manifest, manifest.to_json())
        os.replace(partial, destination)
        os.replace(partial_manifest, manifest_path)
        verification = verify_backup(destination)
        if not verification.valid:
            raise DatabaseBackupError("已发布备份验证失败")
        return manifest
    except OSError as exc:
        raise DatabaseBackupError(f"备份复制失败: {exc}") from exc
    finally:
        _discard_partial(partial)
        _discard_partial(partial_manifest)


def verify_backup(backup: Path) -> BackupVerification:
    backup = backup.resolve()
    manifest_path = backup_manifest_path(backup)
    if not backup.is_file():
        raise DatabaseBackupError("备份文件不存在")
    if not manifest_path.is_file():
        raise DatabaseBackupError("备份 manifest 不存在")
    manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    identity = file_identity(backup)
    schema_version, integrity_check, foreign_key_violations = _database_checks(backup)
    verification = BackupVerification(
        manifest,
        identity,
        schema_version,
        integrity_check,
        foreign_key_violations,
    )
    if not verification.valid:
        raise DatabaseBackupError("备份或 manifest 校验失败")
    return verification


def restore_verified_backup(
    backup: Path, destination: Path, *, production_path: Path
) -> RestoreEvidence:
    """Restore only from a verified backup and never onto the protected production path."""
    backup = backup.resolve()
    destination = destination.resolve()
    production_path = production_path.resolve()
    if destination in {production_path, DEFAULT_PRODUCTION_DATABASE}:
        raise DatabaseBackupError("禁止覆盖正式数据库")
    if destination == backup:
        raise DatabaseBackupError("恢复目标不能覆盖备份")
    if destination.exists():
        raise DatabaseBackupError("恢复目标已存在，拒绝覆盖")
    verification = verify_backup(backup)
    partial = _partial_path(destination)
    try:
        shutil.copyfile(backup, partial)
        restored_identity = file_identity(partial)
        if (
            restored_identity.size != verification.backup_identity.size
            or restored_identity.sha256 != verification.backup_identity.sha256
        ):
            raise DatabaseBackupError("恢复字节校验失败，拒绝发布恢复目标")
        schema_version, integrity_check, foreign_key_violations = _database_checks(partial)
        if integrity_check != "ok" or foreign_key_violations != 0:
            raise DatabaseBackupError("恢复 SQLite 完整性校验失败")
        os.replace(partial, destination)
        return RestoreEvidence(
            source_sha256=verification.manifest.source_sha256,
            restored_sha256=restored_identity.sha256,
            source_schema_version=verification.manifest.source_schema_version,
            restored_schema_version=schema_version,
            integrity_check=integrity_check,
            foreign_key_violations=foreign_key_violations,
            business_digests_match=business_data_digests(backup)
            == business_data_digests(destination),
        )
    except OSError as exc:
        raise DatabaseBackupError(f"恢复复制失败: {exc}") from exc
    finally:
        _discard_partial(partial)


def file_identity(path: Path) -> DatabaseFileIdentity:
    path = path.resolve()
    if not path.is_file():
        raise DatabaseBackupError("数据库文件不存在")
    stat = path.stat()
    return DatabaseFileIdentity(
        path=str(path),
        size=stat.st_size,
        mtime=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        sha256=_sha256(path),
    )


def backup_manifest_path(backup: Path) -> Path:
    return backup.with_name(f"{backup.name}.manifest.json")


def business_data_digests(path: Path) -> tuple[BusinessDataDigest, ...]:
    path = path.resolve()
    _reject_production_sqlite_access(path)
    connection = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        digests: list[BusinessDataDigest] = []
        for table in CRITICAL_BUSINESS_TABLES:
            if table not in tables:
                continue
            columns = tuple(
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(
                f'SELECT {quoted_columns} FROM "{table}" ORDER BY {quoted_columns}'
            ).fetchall()
            payload = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
            digests.append(
                BusinessDataDigest(
                    table, len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest()
                )
            )
        return tuple(digests)
    except sqlite3.Error as exc:
        raise DatabaseBackupError(f"业务数据摘要失败: {exc}") from exc
    finally:
        connection.close()


def restore_evidence(backup: Path, restored: Path) -> RestoreEvidence:
    verification = verify_backup(backup)
    restored_identity = file_identity(restored)
    schema_version, integrity_check, foreign_key_violations = _database_checks(restored)
    return RestoreEvidence(
        source_sha256=verification.manifest.source_sha256,
        restored_sha256=restored_identity.sha256,
        source_schema_version=verification.manifest.source_schema_version,
        restored_schema_version=schema_version,
        integrity_check=integrity_check,
        foreign_key_violations=foreign_key_violations,
        business_digests_match=business_data_digests(backup) == business_data_digests(restored),
    )


def _reject_uncheckpointed_wal(source: Path) -> None:
    """Refuse raw file copies while committed WAL content is not checkpointed."""
    wal = source.with_name(f"{source.name}-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise DatabaseBackupError(
            "源数据库存在未检查点的 WAL 内容，原始复制会丢失已提交数据；"
            "请先 checkpoint 或使用 SQLite 在线备份"
        )


def _validate_backup_paths(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise DatabaseBackupError("数据库文件不存在")
    if source == destination:
        raise DatabaseBackupError("备份目标不能覆盖源数据库")
    if destination == DEFAULT_PRODUCTION_DATABASE:
        raise DatabaseBackupError("禁止写入正式数据库路径")
    manifest_path = backup_manifest_path(destination)
    if destination.exists() or manifest_path.exists():
        raise DatabaseBackupError("备份目标或 manifest 已存在，拒绝覆盖")
    destination.parent.mkdir(parents=True, exist_ok=True)


def _database_checks(path: Path) -> tuple[int, str, int]:
    try:
        _reject_production_sqlite_access(path)
        status = MigrationManager(path).status()
        connection = sqlite3.connect(path)
        try:
            integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()
        return status.current_version, integrity_check, foreign_key_violations
    except (sqlite3.Error, MigrationError) as exc:
        raise DatabaseBackupError(f"SQLite 校验失败: {exc}") from exc


def _partial_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid4().hex}.partial")


def _reject_production_sqlite_access(path: Path) -> None:
    if path.resolve() == DEFAULT_PRODUCTION_DATABASE:
        raise DatabaseBackupError("禁止通过 SQLite 打开正式数据库")


def _write_new_file(path: Path, content: str) -> None:
    if path.exists():
        raise DatabaseBackupError("临时 manifest 已存在")
    path.write_text(content, encoding="utf-8")


def _discard_partial(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
