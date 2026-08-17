"""Fail-closed administrative closure for stale runs missing a baseline."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from app.storage.database import Database


@dataclass(frozen=True, slots=True)
class StaleRunClosureResult:
    run_id: str
    recovery_id: str
    closed: bool
    blockers: tuple[str, ...]


class HeartbeatState(StrEnum):
    PRESENT_FRESH = "present_fresh"
    PRESENT_EXPIRED = "present_expired"
    MISSING_UNEXPECTED = "missing_unexpected"
    INVALID_TIMESTAMP = "invalid_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"


class StaleRunAdministrativeClosureService:
    def __init__(self, database: Database, now: datetime | None = None) -> None:
        self.database = database
        self.now = now or datetime.now(UTC)

    def close(self, run_id: str, explicit_reason: str) -> StaleRunClosureResult:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM continuous_demo_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("run does not exist")
            blockers = self._blockers(connection, run_id, run)
            if blockers:
                return StaleRunClosureResult(run_id, "", False, tuple(blockers))
            recovery_id = uuid4().hex
            limitation = "historical_shadow_account_baseline_missing; full historical balance reconciliation is impossible"
            connection.execute(
                """INSERT INTO continuous_run_recoveries
                (recovery_id,run_id,status,started_at,completed_at,original_run_status,final_run_status,
                 lock_status,database_status,reconciliation_status,external_activity_status,blockers_json,
                 warnings_json,closure_type,historical_baseline_available,
                 historical_balance_reconciliation_possible,closure_limitations,evidence_level)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    recovery_id,
                    run_id,
                    "administratively_closed",
                    self.now.isoformat(),
                    self.now.isoformat(),
                    run["status"],
                    "administratively_closed",
                    "released_or_not_owned",
                    "integrity_checked",
                    "incomplete",
                    "none",
                    "[]",
                    "[]",
                    "administrative_stale_closure",
                    0,
                    0,
                    limitation,
                    "local_database_only",
                ),
            )
            connection.execute(
                """UPDATE continuous_demo_runs SET status='administratively_closed', stopped_at=?,
                stop_reason=?, recovery_required=0, reconciliation_status='incomplete' WHERE run_id=?""",
                (self.now.isoformat(), explicit_reason, run_id),
            )
            connection.execute(
                """UPDATE continuous_run_locks SET released_at=?, release_reason='administrative_stale_closure'
                WHERE run_id=? AND released_at IS NULL""",
                (self.now.isoformat(), run_id),
            )
            connection.execute(
                "INSERT INTO continuous_demo_run_events (run_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                (
                    run_id,
                    "administratively_closed",
                    json.dumps({"recovery_id": recovery_id, "reason": explicit_reason}),
                    self.now.isoformat(),
                ),
            )
            return StaleRunClosureResult(run_id, recovery_id, True, ())

    def _blockers(self, connection: sqlite3.Connection, run_id: str, run: sqlite3.Row) -> list[str]:
        blockers: list[str] = []
        heartbeat_state = self._heartbeat_state(run["last_heartbeat_at"])
        if heartbeat_state is HeartbeatState.PRESENT_FRESH:
            blockers.append("heartbeat_present_and_fresh")
        elif heartbeat_state is not HeartbeatState.PRESENT_EXPIRED:
            blockers.append(f"heartbeat_{heartbeat_state.value}")
        if run["unknown_order_count"]:
            blockers.append("unresolved_unknown_order")
        if connection.execute(
            "SELECT 1 FROM shadow_account_baselines WHERE run_id=?", (run_id,)
        ).fetchone():
            blockers.append("historical_baseline_present")
        if connection.execute("SELECT 1 FROM orders WHERE run_id=?", (run_id,)).fetchone():
            blockers.append("local_order_present")
        if connection.execute("SELECT 1 FROM fills WHERE run_id=?", (run_id,)).fetchone():
            blockers.append("local_fill_present")
        lock = connection.execute(
            "SELECT * FROM continuous_run_locks WHERE run_id=? AND released_at IS NULL", (run_id,)
        ).fetchone()
        if lock:
            if datetime.fromisoformat(str(lock["lease_expires_at"])) >= self.now:
                blockers.append("lease_not_expired")
            if str(lock["host_id"]) == socket.gethostname() and self._process_exists(
                int(lock["process_id"])
            ):
                blockers.append("owner_process_exists")
        return blockers

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

    @staticmethod
    def _process_exists(process_id: int) -> bool:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
