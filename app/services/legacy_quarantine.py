"""Fail-closed isolation of legacy runtime records without mutating them."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.services.stale_run_administrative_closure import HeartbeatState
from app.storage.database import Database


class ProductionDatabaseAccessFromTestError(RuntimeError):
    """Raised when a test accidentally points quarantine code at the real database."""


class RuntimeGenerationGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QuarantineAssessment:
    legacy_run_id: str
    eligible: bool
    heartbeat_state: HeartbeatState
    blockers: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StartupGateResult:
    allowed: bool
    blockers: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class RuntimeGenerationService:
    def __init__(self, database: Database, now: datetime | None = None) -> None:
        _reject_production_database_from_test(database)
        self.database, self.now = database, now or _utc_now()

    def create_preparing(
        self,
        manifest_sha256: str,
        database_sha256_before: str,
        authorization: dict[str, Any],
        notes: str,
    ) -> str:
        generation_id = uuid4().hex
        with self.database.connect() as connection:
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation_number), 0) + 1 FROM runtime_generations"
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO runtime_generations
                (generation_id,generation_number,status,created_at,manifest_sha256,database_sha256_before,authorization_json,notes)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    number,
                    "preparing",
                    self.now.isoformat(),
                    manifest_sha256,
                    database_sha256_before,
                    _canonical_json(authorization),
                    notes,
                ),
            )
        return generation_id

    def activate(self, generation_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT generation_id FROM runtime_generations WHERE status='active'"
            ).fetchone()
            if current is not None and str(current[0]) != generation_id:
                raise RuntimeGenerationGateError("another runtime generation is already active")
            generation = connection.execute(
                "SELECT authorization_json FROM runtime_generations WHERE generation_id=? AND status='preparing'",
                (generation_id,),
            ).fetchone()
            if generation is None:
                raise RuntimeGenerationGateError("generation is absent or not preparing")
            authorization = json.loads(str(generation["authorization_json"]))
            expected_runs = authorization.get("legacy_run_ids")
            auxiliary_runs = authorization.get("auxiliary_legacy_run_ids", [])
            lock_run_id = authorization.get("legacy_lock_run_id")
            if expected_runs is not None:
                expected = {str(item) for item in expected_runs}
                auxiliary = {str(item) for item in auxiliary_runs}
                if expected & auxiliary:
                    raise RuntimeGenerationGateError("legacy and auxiliary quarantine sets overlap")
                quarantined = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT legacy_run_id FROM legacy_run_quarantines WHERE generation_id=?",
                        (generation_id,),
                    )
                }
                if expected | auxiliary != quarantined:
                    raise RuntimeGenerationGateError(
                        "authorized legacy runs are not fully quarantined"
                    )
                if (
                    not isinstance(lock_run_id, str)
                    or not connection.execute(
                        "SELECT 1 FROM legacy_lock_quarantines WHERE generation_id=? AND legacy_run_id=?",
                        (generation_id, lock_run_id),
                    ).fetchone()
                ):
                    raise RuntimeGenerationGateError("authorized legacy lock is not quarantined")
                if connection.execute(
                    "SELECT 1 FROM generation_drift_events WHERE generation_id=? AND severity='blocker'",
                    (generation_id,),
                ).fetchone():
                    raise RuntimeGenerationGateError("legacy drift blocks generation activation")
            updated = connection.execute(
                "UPDATE runtime_generations SET status='active',activated_at=? WHERE generation_id=? AND status='preparing'",
                (self.now.isoformat(), generation_id),
            ).rowcount
            if updated != 1:
                raise RuntimeGenerationGateError("generation is absent or not preparing")

    def require_active_generation(self) -> str:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT generation_id FROM runtime_generations WHERE status='active'"
            ).fetchall()
        if len(rows) != 1:
            raise RuntimeGenerationGateError("exactly one active runtime generation is required")
        return str(rows[0][0])


class LegacyRunQuarantineService:
    def __init__(self, database: Database, now: datetime | None = None) -> None:
        _reject_production_database_from_test(database)
        self.database, self.now = database, now or _utc_now()

    def assess(
        self, legacy_run_id: str, exchange_verification: dict[str, Any]
    ) -> QuarantineAssessment:
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT * FROM continuous_demo_runs WHERE run_id=?", (legacy_run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("legacy run does not exist")
            blockers: list[str] = []
            heartbeat = self._heartbeat_state(run["last_heartbeat_at"])
            if heartbeat is HeartbeatState.PRESENT_FRESH:
                blockers.append("heartbeat_present_and_fresh")
            elif heartbeat in {HeartbeatState.INVALID_TIMESTAMP, HeartbeatState.FUTURE_TIMESTAMP}:
                blockers.append(f"heartbeat_{heartbeat.value}")
            if run["generation_id"] is not None:
                blockers.append("run_already_associated_with_generation")
            if int(run["unknown_order_count"]):
                blockers.append("unresolved_unknown_order")
            external_activity_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM continuous_external_activities WHERE run_id=?",
                    (legacy_run_id,),
                ).fetchone()[0]
            )
            external_activity_present = external_activity_count or str(
                run["stop_reason"] or ""
            ).startswith("external_")
            if external_activity_present and not exchange_verification.get(
                "external_activity_fully_excluded", False
            ):
                blockers.append("historical_external_activity_present")
            checks = (
                ("orders", "run_id", "local_order_present"),
                ("fills", "run_id", "local_fill_present"),
                ("demo_order_proposals", "run_id", "proposal_present"),
                ("bounded_submission_events", "run_id", "submission_event_present"),
            )
            counts: dict[str, int] = {}
            for table, field, blocker in checks:
                count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {field}=?", (legacy_run_id,)
                    ).fetchone()[0]
                )
                counts[table] = count
                if count:
                    blockers.append(blocker)
            lock = connection.execute(
                "SELECT * FROM continuous_run_locks WHERE run_id=? AND released_at IS NULL",
                (legacy_run_id,),
            ).fetchone()
            if lock is not None:
                if self._lease_live(lock["lease_expires_at"]):
                    blockers.append("lease_not_expired")
                if str(lock["host_id"]) == socket.gethostname() and self._process_exists(
                    int(lock["process_id"])
                ):
                    blockers.append("owner_process_exists")
            if exchange_verification.get("historical_evidence_found") is not False:
                blockers.append("exchange_verification_not_clean")
            counts["continuous_external_activities"] = external_activity_count
            evidence = {
                "run": dict(run),
                "related_record_counts": counts,
                "lock": dict(lock) if lock else None,
                "exchange_verification": exchange_verification,
            }
        return QuarantineAssessment(
            legacy_run_id, not blockers, heartbeat, tuple(blockers), evidence
        )

    def quarantine(
        self,
        assessment: QuarantineAssessment,
        generation_id: str,
        manifest_sha256: str,
        snapshot_sha256: str,
        authorization: dict[str, Any],
    ) -> str:
        if not assessment.eligible:
            raise RuntimeGenerationGateError("legacy run is not eligible for quarantine")
        _require_generation_for_quarantine(self.database, generation_id)
        quarantine_id = uuid4().hex
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT quarantine_id FROM legacy_run_quarantines WHERE legacy_run_id=?",
                (assessment.legacy_run_id,),
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            current = connection.execute(
                "SELECT * FROM continuous_demo_runs WHERE run_id=?", (assessment.legacy_run_id,)
            ).fetchone()
            if current is None or dict(current) != assessment.evidence["run"]:
                raise RuntimeGenerationGateError("legacy run changed after assessment")
            connection.execute(
                """INSERT INTO legacy_run_quarantines
                (quarantine_id,legacy_run_id,generation_id,quarantined_at,manifest_sha256,snapshot_sha256,
                 original_status,heartbeat_state,assessment_json,exchange_verification_json,operator_authorization_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    quarantine_id,
                    assessment.legacy_run_id,
                    generation_id,
                    self.now.isoformat(),
                    manifest_sha256,
                    snapshot_sha256,
                    str(current["status"]),
                    assessment.heartbeat_state.value,
                    _canonical_json(assessment.evidence),
                    _canonical_json(assessment.evidence["exchange_verification"]),
                    _canonical_json(authorization),
                ),
            )
        return quarantine_id

    def _heartbeat_state(self, value: object) -> HeartbeatState:
        if value is None:
            return HeartbeatState.MISSING_UNEXPECTED
        try:
            timestamp = datetime.fromisoformat(str(value))
        except ValueError:
            return HeartbeatState.INVALID_TIMESTAMP
        if timestamp.tzinfo is None:
            return HeartbeatState.INVALID_TIMESTAMP
        timestamp = timestamp.astimezone(UTC)
        if timestamp > self.now:
            return HeartbeatState.FUTURE_TIMESTAMP
        return (
            HeartbeatState.PRESENT_FRESH
            if timestamp >= self.now
            else HeartbeatState.PRESENT_EXPIRED
        )

    def _lease_live(self, value: object) -> bool:
        try:
            expiry = datetime.fromisoformat(str(value)).astimezone(UTC)
        except ValueError:
            return True
        return expiry >= self.now

    @staticmethod
    def _process_exists(process_id: int) -> bool:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            # Windows reports ERROR_INVALID_PARAMETER / ERROR_NOT_FOUND for a PID
            # that no longer exists, rather than ProcessLookupError.
            return not (os.name == "nt" and getattr(exc, "winerror", None) in {87, 1168})
        return True


class LegacyLockQuarantineService:
    def __init__(self, database: Database, now: datetime | None = None) -> None:
        _reject_production_database_from_test(database)
        self.database, self.now = database, now or _utc_now()

    def quarantine(self, legacy_run_id: str, generation_id: str, manifest_sha256: str) -> str:
        _require_generation_for_quarantine(self.database, generation_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM legacy_run_quarantines WHERE legacy_run_id=?", (legacy_run_id,)
            ).fetchone():
                raise RuntimeGenerationGateError("legacy run must be quarantined before its lock")
            lock = connection.execute(
                "SELECT * FROM continuous_run_locks WHERE run_id=? AND released_at IS NULL",
                (legacy_run_id,),
            ).fetchone()
            if lock is None:
                raise RuntimeGenerationGateError("unreleased legacy lock does not exist")
            if LegacyRunQuarantineService(self.database, self.now)._lease_live(
                lock["lease_expires_at"]
            ):
                raise RuntimeGenerationGateError("legacy lock lease has not expired")
            if str(
                lock["host_id"]
            ) == socket.gethostname() and LegacyRunQuarantineService._process_exists(
                int(lock["process_id"])
            ):
                raise RuntimeGenerationGateError("legacy lock owner process exists")
            existing = connection.execute(
                "SELECT quarantine_id FROM legacy_lock_quarantines WHERE legacy_run_id=?",
                (legacy_run_id,),
            ).fetchone()
            if existing:
                return str(existing[0])
            quarantine_id = uuid4().hex
            connection.execute(
                """INSERT INTO legacy_lock_quarantines
                (quarantine_id,lock_name,legacy_run_id,generation_id,quarantined_at,manifest_sha256,original_lock_json,assessment_json)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    quarantine_id,
                    str(lock["lock_name"]),
                    legacy_run_id,
                    generation_id,
                    self.now.isoformat(),
                    manifest_sha256,
                    _canonical_json(dict(lock)),
                    _canonical_json({"lease_expired": True, "owner_process_absent": True}),
                ),
            )
        return quarantine_id


class LegacyQuarantineDriftMonitor:
    def __init__(self, database: Database, now: datetime | None = None) -> None:
        _reject_production_database_from_test(database)
        self.database, self.now = database, now or _utc_now()

    def check_once(self, generation_id: str) -> int:
        _require_generation_for_quarantine(self.database, generation_id)
        events: list[tuple[str, str, dict[str, Any]]] = []
        with self.database.connect() as connection:
            quarantines = connection.execute(
                "SELECT legacy_run_id,assessment_json FROM legacy_run_quarantines WHERE generation_id=?",
                (generation_id,),
            ).fetchall()
            for quarantine in quarantines:
                baseline = json.loads(str(quarantine["assessment_json"]))["run"]
                current = connection.execute(
                    "SELECT * FROM continuous_demo_runs WHERE run_id=?",
                    (quarantine["legacy_run_id"],),
                ).fetchone()
                if current is None:
                    events.append((str(quarantine["legacy_run_id"]), "legacy_run_deleted", {}))
                elif dict(current) != baseline:
                    events.append(
                        (
                            str(quarantine["legacy_run_id"]),
                            "legacy_run_changed",
                            {"baseline": baseline, "current": dict(current)},
                        )
                    )
            for run_id, event_type, evidence in events:
                connection.execute(
                    "INSERT INTO generation_drift_events (generation_id,legacy_run_id,event_type,detected_at,evidence_json,severity) VALUES (?,?,?,?,?,?)",
                    (
                        generation_id,
                        run_id,
                        event_type,
                        self.now.isoformat(),
                        _canonical_json(evidence),
                        "blocker",
                    ),
                )
        return len(events)


class RuntimeStartupGate:
    def __init__(self, database: Database) -> None:
        _reject_production_database_from_test(database)
        self.database = database

    def check(
        self, expected_legacy_run_ids: tuple[str, ...], legacy_lock_run_id: str
    ) -> StartupGateResult:
        blockers: list[str] = []
        try:
            generation_id = RuntimeGenerationService(self.database).require_active_generation()
        except RuntimeGenerationGateError as exc:
            return StartupGateResult(False, (str(exc),))
        with self.database.connect() as connection:
            actual = {
                str(row[0])
                for row in connection.execute(
                    "SELECT legacy_run_id FROM legacy_run_quarantines WHERE generation_id=?",
                    (generation_id,),
                )
            }
            missing = set(expected_legacy_run_ids) - actual
            if missing:
                blockers.append("legacy_runs_not_fully_quarantined")
            if not connection.execute(
                "SELECT 1 FROM legacy_lock_quarantines WHERE generation_id=? AND legacy_run_id=?",
                (generation_id, legacy_lock_run_id),
            ).fetchone():
                blockers.append("legacy_lock_not_quarantined")
            if connection.execute(
                "SELECT 1 FROM generation_drift_events WHERE generation_id=? AND severity='blocker'",
                (generation_id,),
            ).fetchone():
                blockers.append("legacy_drift_detected")
        return StartupGateResult(not blockers, tuple(blockers))


def _require_active_generation(database: Database, generation_id: str) -> None:
    if RuntimeGenerationService(database).require_active_generation() != generation_id:
        raise RuntimeGenerationGateError("requested generation is not active")


def _require_generation_for_quarantine(database: Database, generation_id: str) -> None:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM runtime_generations WHERE generation_id=?", (generation_id,)
        ).fetchone()
    if row is None or str(row["status"]) not in {"preparing", "active"}:
        raise RuntimeGenerationGateError("generation is absent or not available for quarantine")


def _reject_production_database_from_test(database: Database) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST") and database.path == Path("data/trading.db").resolve():
        raise ProductionDatabaseAccessFromTestError("tests may not access data/trading.db")
