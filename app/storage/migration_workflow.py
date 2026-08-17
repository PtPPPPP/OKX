"""Controlled, fail-closed database migration workflow.

Two explicit phases connect the CLI to the existing migration safety model:

1. ``build_migration_plan`` — strictly read-only rehearsal/plan. It never
   mutates the database and never issues an authorization.
2. ``execute_authorized_migration`` — re-validates the plan against the live
   database (TOCTOU), produces fresh backup/restore/rehearsal evidence, then
   hands control to the existing gate + authorized production executor.

No generic bypass flags exist: without an operator-issued authorization bound
to the exact database identity, schema versions and plan hash, execution is
blocked.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage import migrations as migrations_module
from app.storage.database_backup import (
    DatabaseBackupError,
    create_verified_backup,
    file_identity,
    restore_evidence,
    restore_verified_backup,
    verify_backup,
)
from app.storage.migration_execution import (
    MigrationExecutionError,
    execute_authorized_production_migration,
)
from app.storage.migration_gate import (
    CURRENT_SCHEMA_VERSION,
    MigrationAuthorizationError,
    MigrationRehearsalEvidence,
    evaluate_database_migration_preflight,
    issue_migration_execution_authorization,
)
from app.storage.migrations import MigrationError, MigrationManager

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PLAN_REQUIRED_KEYS = (
    "database_path",
    "database_sha256",
    "current_schema_version",
    "target_schema_version",
    "plan_sha256",
)

_ACTIVE_RUN_STATUSES = ("starting", "warming_up", "shadow_running", "running")

_OS_CRASH_CHILD = """
from pathlib import Path
import sys

from app.storage.migrations import MigrationManager

database = Path(sys.argv[1])
target = int(sys.argv[2])
marker = Path(sys.argv[3])
MigrationManager(database).migrate(backup=False, target_version=target)
marker.write_text("committed", encoding="ascii")
sys.stdin.buffer.read()
"""


class MigrationWorkflowError(RuntimeError):
    """Blocked migration with an operator-actionable explanation."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class MigrationPlanReport:
    database_path: str
    database_exists: bool
    database_sha256: str
    current_schema_version: int
    target_schema_version: int
    pending_migrations: tuple[str, ...]
    compatibility_status: str
    backup_required: bool
    authorization_required: bool
    runtime_active: bool
    blockers: tuple[str, ...]
    risk_summary: tuple[str, ...]
    plan_sha256: str

    def payload(self) -> dict[str, Any]:
        if self.authorization_required and not self.blockers:
            next_step = (
                "python -m app db-migrate-plan --output plan.json, then "
                "python -m app db-migrate-authorized --plan plan.json "
                "--operator-confirmation-id <approval-id>"
            )
        else:
            next_step = "resolve blockers before planning an authorized migration"
        return {
            "database_path": self.database_path,
            "database_exists": self.database_exists,
            "database_sha256": self.database_sha256,
            "current_schema_version": self.current_schema_version,
            "target_schema_version": self.target_schema_version,
            "pending_migrations": list(self.pending_migrations),
            "compatibility_status": self.compatibility_status,
            "backup_required": self.backup_required,
            "authorization_required": self.authorization_required,
            "runtime_active": self.runtime_active,
            "blockers": list(self.blockers),
            "risk_summary": list(self.risk_summary),
            "plan_sha256": self.plan_sha256,
            "next_step": next_step,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedMigrationOutcome:
    result: str
    database_path: str
    database_sha256: str
    from_version: int
    to_version: int
    applied_migrations: tuple[str, ...]
    backup_path: str
    audit_path: str
    plan_sha256: str
    operator_confirmation_id: str

    def payload(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "database_path": self.database_path,
            "database_sha256": self.database_sha256,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "applied_migrations": list(self.applied_migrations),
            "backup_path": self.backup_path,
            "audit_path": self.audit_path,
            "plan_sha256": self.plan_sha256,
            "operator_confirmation_id": self.operator_confirmation_id,
        }


def _plan_hash(database_path: Path, database_sha256: str, current: int, target: int) -> str:
    pending = [
        {"version": m.version, "name": m.name, "checksum": m.checksum}
        for m in migrations_module.MIGRATIONS
        if current < m.version <= target
    ]
    canonical = json.dumps(
        {
            "database_path": str(database_path),
            "database_sha256": database_sha256,
            "current_schema_version": current,
            "target_schema_version": target,
            "pending": pending,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _active_runtime(database_path: Path) -> bool:
    """Best-effort fail-closed detection of an active continuous/shadow run."""
    if not database_path.exists():
        return False
    try:
        with sqlite3.connect(database_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            placeholders = ", ".join("?" for _ in _ACTIVE_RUN_STATUSES)
            if "continuous_demo_runs" in tables:
                active = connection.execute(
                    f"""SELECT COUNT(*) FROM continuous_demo_runs
                    WHERE status IN ({placeholders})""",
                    _ACTIVE_RUN_STATUSES,
                ).fetchone()[0]
                if active:
                    return True
            if "continuous_run_locks" in tables:
                unreleased = connection.execute(
                    "SELECT COUNT(*) FROM continuous_run_locks WHERE released_at IS NULL"
                ).fetchone()[0]
                if unreleased:
                    return True
            if "trading_heartbeats" in tables:
                live = connection.execute(
                    """SELECT COUNT(*) FROM trading_heartbeats
                    WHERE lease_expires_at > ?""",
                    (datetime.now(UTC).isoformat(),),
                ).fetchone()[0]
                if live:
                    return True
            return False
    except sqlite3.Error as exc:
        raise MigrationWorkflowError(
            "runtime_state_unreadable",
            f"无法读取数据库运行状态，按 fail-closed 拒绝迁移: {exc}",
        ) from exc


def build_migration_plan(database_path: Path) -> MigrationPlanReport:
    """Read-only migration plan; never mutates the database."""
    database_path = database_path.resolve()
    exists = database_path.is_file()
    if not exists:
        current = 0
        compatibility = "fresh_database"
        blockers: tuple[str, ...] = ("database_is_missing",)
        sha = ""
    else:
        try:
            status = MigrationManager(database_path).status()
        except MigrationError as exc:
            raise MigrationWorkflowError(
                "database_status_unreadable",
                f"数据库迁移状态不可读（可能已损坏）: {exc}",
            ) from exc
        current = status.current_version
        if current > CURRENT_SCHEMA_VERSION:
            compatibility = "database_newer_than_application"
            blockers = ("database_schema_is_newer_than_application",)
        elif not status.compatible:
            compatibility = "incompatible"
            blockers = ("schema_history_checksum_or_metadata_invalid",)
        else:
            compatibility = "compatible"
            blockers = ()
        sha = file_identity(database_path).sha256
    pending = tuple(
        m.name
        for m in migrations_module.MIGRATIONS
        if current < m.version <= CURRENT_SCHEMA_VERSION
    )
    runtime_active = _active_runtime(database_path)
    if runtime_active:
        blockers = (*blockers, "runtime_is_active")
    risk: list[str] = []
    if pending:
        risk.append(f"pending_migrations={len(pending)}")
        risk.append("verified_backup_is_created_before_migration")
        risk.append("restore_and_rehearsal_are_re_executed_at_authorization_time")
        risk.append("all_runtime_must_be_stopped")
    else:
        risk.append("no_pending_migrations_noop")
    return MigrationPlanReport(
        str(database_path),
        exists,
        sha,
        current,
        CURRENT_SCHEMA_VERSION,
        pending,
        compatibility,
        backup_required=exists and bool(pending),
        authorization_required=bool(pending),
        runtime_active=runtime_active,
        blockers=blockers,
        risk_summary=tuple(risk),
        plan_sha256=_plan_hash(database_path, sha, current, CURRENT_SCHEMA_VERSION),
    )


def load_plan_file(plan_file: Path) -> dict[str, Any]:
    try:
        raw = json.loads(plan_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MigrationWorkflowError(
            "plan_file_unreadable",
            f"无法读取迁移计划文件 {plan_file}: {exc}；"
            "请先运行 python -m app db-migrate-plan --output plan.json",
        ) from exc
    except json.JSONDecodeError as exc:
        raise MigrationWorkflowError(
            "plan_file_invalid",
            f"迁移计划文件不是有效 JSON: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise MigrationWorkflowError("plan_file_invalid", "迁移计划文件必须是 JSON 对象")
    missing = [key for key in _PLAN_REQUIRED_KEYS if key not in raw]
    if missing:
        raise MigrationWorkflowError(
            "plan_file_invalid",
            f"迁移计划文件缺少必需字段 {missing}；"
            "请重新运行 python -m app db-migrate-plan --output plan.json",
        )
    if not isinstance(raw["current_schema_version"], int) or not isinstance(
        raw["target_schema_version"], int
    ):
        raise MigrationWorkflowError(
            "plan_file_invalid",
            "迁移计划文件版本字段类型无效",
        )
    return raw


def _audit_path(database_path: Path) -> Path:
    return database_path.parent / "migration_audit.jsonl"


def _append_audit(database_path: Path, record: dict[str, Any]) -> None:
    payload = dict(record)
    payload.setdefault("recorded_at", datetime.now(UTC).isoformat())
    payload.setdefault("database_path", str(database_path))
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    with _audit_path(database_path).open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def _validate_plan_against_database(
    *, database_path: Path, plan: dict[str, Any], operator_confirmation_id: str
) -> MigrationPlanReport:
    if not operator_confirmation_id.strip():
        raise MigrationWorkflowError(
            "operator_confirmation_missing",
            "缺少 --operator-confirmation-id：授权迁移要求显式的操作确认标识（例如审批单号）",
        )
    report = build_migration_plan(database_path)
    if plan["database_path"] != report.database_path:
        raise MigrationWorkflowError(
            "authorization_database_mismatch",
            f"计划绑定的数据库是 {plan['database_path']}，当前配置的数据库是 "
            f"{report.database_path}；请对正确的数据库重新生成计划",
        )
    if plan["database_sha256"] != report.database_sha256:
        raise MigrationWorkflowError(
            "authorization_stale_database_changed",
            "授权后数据库已发生变化（sha256 不匹配），授权失效；数据库当前版本 "
            f"{report.current_schema_version}，目标版本 {report.target_schema_version}；"
            "请重新运行 db-migrate-plan 并重新授权",
        )
    if plan["current_schema_version"] != report.current_schema_version:
        raise MigrationWorkflowError(
            "authorization_from_version_mismatch",
            f"计划授权的起始版本是 {plan['current_schema_version']}，数据库当前版本是 "
            f"{report.current_schema_version}；请重新生成计划",
        )
    if plan["target_schema_version"] != CURRENT_SCHEMA_VERSION:
        raise MigrationWorkflowError(
            "authorization_target_mismatch",
            f"计划授权的目标版本是 {plan['target_schema_version']}，应用程序支持的目标版本是 "
            f"{CURRENT_SCHEMA_VERSION}；请升级程序后重新生成计划",
        )
    if plan["plan_sha256"] != report.plan_sha256:
        raise MigrationWorkflowError(
            "authorization_plan_changed",
            "迁移计划在授权后发生变化（plan_sha256 不匹配），授权失效；"
            "请重新运行 db-migrate-plan 并重新授权",
        )
    if report.compatibility_status == "database_newer_than_application":
        raise MigrationWorkflowError(
            "database_newer_than_application",
            "数据库 schema 版本高于应用程序，拒绝自动处理；请升级应用程序",
        )
    if report.compatibility_status == "incompatible":
        raise MigrationWorkflowError(
            "schema_history_incompatible",
            "数据库迁移历史/校验和不兼容，拒绝迁移；不会自动修复",
        )
    if report.runtime_active:
        raise MigrationWorkflowError(
            "runtime_is_active",
            "检测到仍在运行的 continuous/shadow 任务，拒绝迁移；请先停止所有运行时",
        )
    return report


def _integrity(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    return integrity, violations


def _business_columns(path: Path) -> dict[str, tuple[str, ...]]:
    """Snapshot the business-table column set before a rehearsal migration."""
    from app.storage.database_backup import CRITICAL_BUSINESS_TABLES

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            table: tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
            for table in CRITICAL_BUSINESS_TABLES
            if table in tables
        }


def _business_digest(path: Path, columns: dict[str, tuple[str, ...]]) -> dict[str, tuple[int, str]]:
    """Digest business rows over a fixed column set, ignoring added columns."""
    result: dict[str, tuple[int, str]] = {}
    with sqlite3.connect(path) as connection:
        for table, names in columns.items():
            quoted = ", ".join(f'"{name}"' for name in names)
            rows = connection.execute(
                f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}'
            ).fetchall()
            payload = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
            result[table] = (len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return result


def _wait_for_marker(marker: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise MigrationWorkflowError(
                "os_crash_child_exited",
                f"崩溃恢复演练子进程提前退出: {stderr}",
            )
        time.sleep(0.05)
    raise MigrationWorkflowError(
        "os_crash_timeout",
        "崩溃恢复演练子进程未达到提交边界",
    )


def _rehearse_os_crash_recovery(
    workspace: Path,
    backup: Path,
    production_path: Path,
    first_target: int,
    to_version: int,
) -> None:
    crash_copy = workspace / "os-crash.db"
    restore_verified_backup(backup, crash_copy, production_path=production_path)
    marker = workspace / "os-crash-committed.txt"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            _OS_CRASH_CHILD,
            str(crash_copy),
            str(first_target),
            str(marker),
        ],
        cwd=_REPO_ROOT,
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
    MigrationManager(crash_copy).migrate(backup=False, target_version=to_version)
    status = MigrationManager(crash_copy).status()
    if status.current_version != to_version or not status.compatible:
        raise MigrationWorkflowError(
            "os_crash_recovery_failed",
            "硬中断后重启迁移未能恢复到目标版本",
        )
    integrity, violations = _integrity(crash_copy)
    if integrity != "ok" or violations:
        raise MigrationWorkflowError(
            "os_crash_recovery_failed",
            "硬中断后恢复的数据库完整性校验失败",
        )


def _rehearse_failure_recovery(
    workspace: Path,
    backup: Path,
    production_path: Path,
    from_version: int,
    to_version: int,
) -> None:
    failing_copy = workspace / "failure-injection.db"
    restore_verified_backup(backup, failing_copy, production_path=production_path)
    columns = _business_columns(failing_copy)
    before = _business_digest(failing_copy, columns)
    lock = sqlite3.connect(failing_copy)
    try:
        lock.execute("BEGIN IMMEDIATE")
        try:
            MigrationManager(failing_copy).migrate(backup=False, target_version=to_version)
        except MigrationError:
            pass
        else:
            raise MigrationWorkflowError(
                "failure_injection_did_not_fail",
                "故障注入迁移意外成功，无法验证回滚语义",
            )
        status = MigrationManager(failing_copy).status()
        if status.current_version != from_version:
            raise MigrationWorkflowError(
                "failure_recovery_failed",
                "失败迁移后数据库版本不应变化",
            )
    finally:
        lock.rollback()
        lock.close()
    MigrationManager(failing_copy).migrate(backup=False, target_version=to_version)
    status = MigrationManager(failing_copy).status()
    if status.current_version != to_version or not status.compatible:
        raise MigrationWorkflowError(
            "failure_recovery_failed",
            "锁释放后重试迁移未能恢复到目标版本",
        )
    if _business_digest(failing_copy, columns) != before:
        raise MigrationWorkflowError(
            "failure_recovery_failed",
            "故障恢复后业务数据摘要不一致",
        )


def _run_full_rehearsal(
    *,
    workspace: Path,
    backup: Path,
    production_path: Path,
    source_sha256: str,
    from_version: int,
    to_version: int,
) -> MigrationRehearsalEvidence:
    """Exercise every rehearsal invariant on throwaway copies; block on any failure."""
    rehearsal_copy = workspace / "rehearsal.db"
    restore_verified_backup(backup, rehearsal_copy, production_path=production_path)
    columns = _business_columns(rehearsal_copy)
    before = _business_digest(rehearsal_copy, columns)
    MigrationManager(rehearsal_copy).migrate(backup=False, target_version=to_version)
    status = MigrationManager(rehearsal_copy).status()
    if status.current_version != to_version or not status.compatible:
        raise MigrationWorkflowError("failed", "演练迁移未达到目标版本或历史校验失败")
    integrity, violations = _integrity(rehearsal_copy)
    if integrity != "ok" or violations:
        raise MigrationWorkflowError("schema_integrity_failed", "演练迁移后完整性校验失败")
    if _business_digest(rehearsal_copy, columns) != before:
        raise MigrationWorkflowError("data_integrity_failed", "演练迁移改变了业务数据")
    if MigrationManager(rehearsal_copy).migrate(backup=False, target_version=to_version) != ():
        raise MigrationWorkflowError("idempotency_failed", "演练迁移重放不是空操作")
    if _business_digest(rehearsal_copy, columns) != before:
        raise MigrationWorkflowError("idempotency_failed", "演练迁移重放改变了业务数据")
    _rehearse_failure_recovery(workspace, backup, production_path, from_version, to_version)
    _rehearse_os_crash_recovery(workspace, backup, production_path, from_version + 1, to_version)
    return MigrationRehearsalEvidence(
        source_sha256,
        from_version,
        to_version,
        True,
        True,
        True,
        True,
        True,
        True,
    )


def execute_authorized_migration(
    *,
    database_path: Path,
    plan: dict[str, Any],
    operator_confirmation_id: str,
) -> AuthorizedMigrationOutcome:
    """Run the controlled migration after full re-validation and evidence collection."""
    from app.storage import database_backup

    database_path = database_path.resolve()
    report = _validate_plan_against_database(
        database_path=database_path,
        plan=plan,
        operator_confirmation_id=operator_confirmation_id,
    )
    audit_base: dict[str, Any] = {
        "database_sha256": report.database_sha256,
        "from_version": report.current_schema_version,
        "to_version": report.target_schema_version,
        "plan_sha256": report.plan_sha256,
        "operator_confirmation_id": operator_confirmation_id,
    }

    if not report.pending_migrations:
        if not report.database_exists:
            raise MigrationWorkflowError(
                "database_is_missing",
                "数据库不存在；受控迁移入口拒绝创建数据库，应用启动时会自动初始化",
            )
        _append_audit(
            database_path, {**audit_base, "event": "authorized_migration", "result": "no_op"}
        )
        return AuthorizedMigrationOutcome(
            "NO_OP",
            report.database_path,
            report.database_sha256,
            report.current_schema_version,
            report.target_schema_version,
            (),
            "",
            str(_audit_path(database_path)),
            report.plan_sha256,
            operator_confirmation_id,
        )

    if str(database_path) != str(database_backup.DEFAULT_PRODUCTION_DATABASE):
        raise MigrationWorkflowError(
            "not_the_protected_production_database",
            "受控授权迁移入口只接受固定正式数据库 "
            f"{database_backup.DEFAULT_PRODUCTION_DATABASE}；"
            "数据库副本请继续使用 DATABASE_URL + db-migrate 流程",
        )

    _append_audit(
        database_path, {**audit_base, "event": "authorized_migration", "result": "started"}
    )
    workspace = Path(tempfile.mkdtemp(prefix="okx-migration-rehearsal-"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.parent / "backups" / f"migration-{stamp}.db"
    try:
        try:
            create_verified_backup(database_path, backup_path)
            backup_verification = verify_backup(backup_path)
            restored = workspace / "restore-rehearsal.db"
            restore_verified_backup(backup_path, restored, production_path=database_path)
            restore = restore_evidence(backup_path, restored)
        except DatabaseBackupError as exc:
            raise MigrationWorkflowError(
                "backup_or_restore_failed_migration_blocked",
                f"备份/恢复演练失败，迁移被阻止: {exc}",
            ) from exc
        try:
            rehearsal = _run_full_rehearsal(
                workspace=workspace,
                backup=backup_path,
                production_path=database_path,
                source_sha256=report.database_sha256,
                from_version=report.current_schema_version,
                to_version=report.target_schema_version,
            )
        except MigrationWorkflowError as exc:
            raise MigrationWorkflowError(
                f"rehearsal_{exc.reason_code}",
                f"迁移演练失败，迁移被阻止: {exc}",
            ) from exc
        except MigrationError as exc:
            raise MigrationWorkflowError(
                "rehearsal_failed",
                f"迁移演练失败，迁移被阻止: {exc}",
            ) from exc
        preflight = evaluate_database_migration_preflight(
            production_path=database_path,
            expected_production_path=database_path,
            expected_source_sha256=report.database_sha256,
            expected_source_schema_version=report.current_schema_version,
            expected_target_schema_version=report.target_schema_version,
            backup=backup_verification,
            restore=restore,
            rehearsal=rehearsal,
            bounded_demo_running=False,
            continuous_shadow_running=False,
        )
        if not preflight.ready:
            raise MigrationWorkflowError(
                "preflight_blocked",
                f"迁移预检未通过: {list(preflight.blockers)}",
            )
        authorization = issue_migration_execution_authorization(
            preflight, operator_confirmation_id=operator_confirmation_id
        )
        try:
            execution = execute_authorized_production_migration(
                database_path=database_path,
                backup_path=backup_path,
                preflight=preflight,
                authorization=authorization,
            )
        except (MigrationError, MigrationExecutionError, MigrationAuthorizationError) as exc:
            raise MigrationWorkflowError(
                "execution_failed",
                f"授权迁移执行失败: {exc}",
            ) from exc
        _append_audit(
            database_path,
            {
                **audit_base,
                "event": "authorized_migration",
                "result": "success",
                "applied_migrations": list(execution.applied_migrations),
                "integrity_check": execution.integrity_check,
                "foreign_key_violations": execution.foreign_key_violations,
            },
        )
        return AuthorizedMigrationOutcome(
            "MIGRATED",
            execution.database_path,
            report.database_sha256,
            execution.source_schema_version,
            execution.target_schema_version,
            execution.applied_migrations,
            str(backup_path),
            str(_audit_path(database_path)),
            report.plan_sha256,
            operator_confirmation_id,
        )
    except MigrationWorkflowError as exc:
        _append_audit(
            database_path,
            {
                **audit_base,
                "event": "authorized_migration",
                "result": "blocked_or_failed",
                "reason_code": exc.reason_code,
                "reason": str(exc),
            },
        )
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


__all__ = [
    "AuthorizedMigrationOutcome",
    "MigrationPlanReport",
    "MigrationWorkflowError",
    "build_migration_plan",
    "execute_authorized_migration",
    "load_plan_file",
]
