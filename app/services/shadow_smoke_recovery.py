"""Auditable local recovery for an externally terminated public Shadow Smoke run."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.storage.database import Database

_INTERRUPTED_STATUSES = frozenset({"starting", "warming_up", "shadow_running"})


@dataclass(frozen=True, slots=True)
class ShadowSmokeRecoveryResult:
    run_id: str
    recovery_id: str | None
    recovered: bool
    final_status: str | None
    recovery_state: str
    blockers: tuple[str, ...]


class ShadowSmokeRecoveryService:
    """Finalize only a dead-owner Shadow Smoke run without exchange access."""

    def __init__(
        self,
        database: Database,
        *,
        now: datetime | None = None,
        process_exists: Callable[[int], bool] | None = None,
    ) -> None:
        self.database = database
        self.now = now or datetime.now(UTC)
        self.process_exists = process_exists or self._process_exists

    def recover(self, run_id: str, reason: str) -> ShadowSmokeRecoveryResult:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM continuous_demo_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("Shadow Smoke run does not exist")
            lock = connection.execute(
                "SELECT * FROM continuous_run_locks WHERE run_id=? AND released_at IS NULL",
                (run_id,),
            ).fetchone()
            blockers = self._blockers(connection, run, lock)
            if blockers:
                state = (
                    "ACTIVE_RUN_LOCKED"
                    if "owner_process_exists" in blockers
                    else "STALE_LOCK_RECOVERY_BLOCKED"
                )
                return ShadowSmokeRecoveryResult(run_id, None, False, None, state, tuple(blockers))

            recovery_id = uuid4().hex
            now = self.now.isoformat()
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
                    "interrupted_recovered",
                    now,
                    now,
                    str(run["status"]),
                    "interrupted",
                    "stale_owner_absent",
                    "integrity_checked",
                    "not_applicable",
                    "none",
                    "[]",
                    "[]",
                    "shadow_smoke_stale_recovery",
                    0,
                    0,
                    "no_private_or_order_activity",
                    "local_database_only",
                ),
            )
            connection.execute(
                """UPDATE continuous_demo_runs
                SET status='interrupted', stopped_at=?, stop_reason=?, recovery_required=0,
                    private_stream_status='not_created', public_stream_status='interrupted'
                WHERE run_id=?""",
                (now, reason, run_id),
            )
            connection.execute(
                """UPDATE continuous_run_locks SET released_at=?, release_reason='stale_owner_absent'
                WHERE run_id=? AND released_at IS NULL""",
                (now, run_id),
            )
            connection.execute(
                """INSERT INTO continuous_demo_run_events
                (run_id,event_type,details_json,created_at) VALUES (?,?,?,?)""",
                (
                    run_id,
                    "shadow_smoke_interrupted_recovered",
                    json.dumps({"recovery_id": recovery_id, "reason": reason}, sort_keys=True),
                    now,
                ),
            )
            return ShadowSmokeRecoveryResult(
                run_id,
                recovery_id,
                True,
                "interrupted",
                "INTERRUPTED_RECOVERED",
                (),
            )

    def _blockers(
        self, connection: sqlite3.Connection, run: sqlite3.Row, lock: sqlite3.Row | None
    ) -> list[str]:
        blockers: list[str] = []
        if str(run["mode"]) != "shadow":
            blockers.append("not_shadow_mode")
        if str(run["status"]) not in _INTERRUPTED_STATUSES:
            blockers.append("run_not_interruptible")
        if lock is None:
            blockers.append("unreleased_lock_missing")
        else:
            if datetime.fromisoformat(str(lock["lease_expires_at"])) >= self.now:
                blockers.append("lease_not_expired")
            if str(lock["host_id"]) == socket.gethostname() and self.process_exists(
                int(lock["process_id"])
            ):
                blockers.append("owner_process_exists")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            blockers.append("database_integrity_check_failed")
        if connection.execute("SELECT 1 FROM orders WHERE run_id=?", (run["run_id"],)).fetchone():
            blockers.append("local_order_present")
        if connection.execute("SELECT 1 FROM fills WHERE run_id=?", (run["run_id"],)).fetchone():
            blockers.append("local_fill_present")
        if connection.execute(
            "SELECT 1 FROM shadow_order_proposals WHERE run_id=? AND submission_performed=1",
            (run["run_id"],),
        ).fetchone():
            blockers.append("submitted_shadow_proposal_present")
        if connection.execute(
            "SELECT 1 FROM private_state_snapshots WHERE received_at>=?", (run["started_at"],)
        ).fetchone():
            blockers.append("private_activity_present")
        return blockers

    @staticmethod
    def _process_exists(process_id: int) -> bool:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as error:
            # Windows reports a missing PID from os.kill(..., 0) as WinError 87.
            # Any other OS failure is treated as live so recovery remains fail-closed.
            return getattr(error, "winerror", None) != 87
        return True
