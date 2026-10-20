"""Local runtime evidence. Observations never authorize trading or reset state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.runtime.clock import Clock, SystemClock
from app.storage.database import Database

ALERT_ACTIONS = {
    "heartbeat_missing_or_expired": "inspect_owner_process_then_use_expired_lease_recovery",
    "public_connection_unconfirmed": "inspect_public_connection_then_validate_continuity",
    "clock_inconsistent": "repair_clock_then_validate_persisted_timestamps",
    "market_stale": "restore_public_feed_then_validate_checkpoint_and_continuity",
    "progress_stalled": "inspect_worker_then_validate_checkpoint_and_continuity",
    "public_disconnected": "bounded_public_reconnect_then_reconcile_gap",
    "checkpoint_invalid": "inspect_checkpoint_and_version_do_not_reset",
    "storage_failure": "repair_storage_then_inspect_last_committed_checkpoint",
    "task_failed": "inspect_task_failure_and_restart_under_supervision",
    "account_untrusted": "coordinator_reconciliation_required",
    "unknown_order": "query_original_client_order_id_and_reconcile_never_resubmit",
}


@dataclass(slots=True)
class RuntimeObservation:
    run_id: str
    mode: str
    strategy_version: str
    started_at: str
    format_version: int = 1
    lifecycle: str = "bootstrapping"
    heartbeat_at: str | None = None
    market_received_at: str | None = None
    confirmed_processed_at: str | None = None
    checkpoint: str | None = None
    public_connection: str = "unknown"
    private_connection: str = "unknown"
    public_reconnects: int | None = 0
    private_reconnects: int | None = None
    reconciliation: str = "unknown"
    account_trusted: bool | None = None
    stop_reason: str | None = None
    heartbeat_timeout_seconds: float = 30
    market_timeout_seconds: float = 120
    progress_timeout_seconds: float = 7320


def assess(observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A fresh heartbeat is evidence of liveness, never of business health."""

    def age(field: str) -> float | None:
        value = observation.get(field)
        if value is None:
            return None
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            raise ValueError("runtime evidence requires timezone-aware timestamps")
        return (now - timestamp).total_seconds()

    heartbeat_age = age("heartbeat_at")
    market_age = age("market_received_at")
    progress_age = age("confirmed_processed_at")
    elapsed = age("started_at")
    reasons: list[str] = []
    alive = (
        heartbeat_age is not None and 0 <= heartbeat_age <= observation["heartbeat_timeout_seconds"]
    )
    if not alive:
        reasons.append("heartbeat_missing_or_expired")
    if any(
        value is not None and value < 0
        for value in (heartbeat_age, market_age, progress_age, elapsed)
    ):
        reasons.append("clock_inconsistent")
    if (market_age if market_age is not None else elapsed or 0) > observation[
        "market_timeout_seconds"
    ]:
        reasons.append("market_stale")
    if (progress_age if progress_age is not None else elapsed or 0) > observation[
        "progress_timeout_seconds"
    ]:
        reasons.append("progress_stalled")
    if observation.get("public_connection") not in {"connected", "local_replay"}:
        reasons.append("public_connection_unconfirmed")
    if observation.get("account_trusted") is False or (
        observation.get("mode") == "shadow_with_private_checks"
        and observation.get("account_trusted") is not True
    ):
        reasons.append("account_untrusted")
    lifecycle = observation.get("lifecycle", "unknown")
    if lifecycle in {"blocked", "failed", "frozen", "interrupted"}:
        reasons.append(str(observation.get("stop_reason") or lifecycle))
    if lifecycle == "stopped":
        health = "stopped"
    elif reasons:
        health = (
            "blocked" if lifecycle in {"blocked", "failed", "frozen", "interrupted"} else "degraded"
        )
    else:
        health = "healthy" if market_age is not None and progress_age is not None else "unknown"
    return {
        "health": health,
        "process_heartbeat_fresh": alive,
        "market_arriving": market_age is not None
        and 0 <= market_age <= observation["market_timeout_seconds"],
        "business_progressing": progress_age is not None
        and 0 <= progress_age <= observation["progress_timeout_seconds"],
        "blocking_reasons": reasons,
        "recovery_actions": sorted({ALERT_ACTIONS[r] for r in reasons if r in ALERT_ACTIONS}),
    }


class RuntimeObserver:
    """One replaceable status row, durable lifecycle events, coalesced alert rows."""

    def __init__(
        self, database: Database, observation: RuntimeObservation, clock: Clock | None = None
    ) -> None:
        self.database = database
        self.state = observation
        self.clock = clock or SystemClock()

    def persist(self) -> None:
        now = self.clock.now().isoformat()
        self.state.heartbeat_at = now
        payload = asdict(self.state)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id,details_json FROM continuous_demo_run_events WHERE run_id=? AND event_type='runtime_observation_v1' ORDER BY id DESC LIMIT 1",
                (self.state.run_id,),
            ).fetchone()
            previous = json.loads(str(row["details_json"])) if row else {}
            if row:
                connection.execute(
                    "UPDATE continuous_demo_run_events SET details_json=?,created_at=? WHERE id=?",
                    (json.dumps(payload, sort_keys=True), now, row["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO continuous_demo_run_events(run_id,event_type,details_json,created_at) VALUES (?,'runtime_observation_v1',?,?)",
                    (self.state.run_id, json.dumps(payload, sort_keys=True), now),
                )
            if previous.get("lifecycle") != self.state.lifecycle:
                connection.execute(
                    "INSERT INTO continuous_demo_run_events(run_id,event_type,details_json,created_at) VALUES (?,'runtime_lifecycle_v1',?,?)",
                    (
                        self.state.run_id,
                        json.dumps(
                            {
                                "from": previous.get("lifecycle", "unknown"),
                                "to": self.state.lifecycle,
                                "reason": self.state.stop_reason,
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )

    def alert(self, code: str, *, active: bool = True) -> None:
        if code not in ALERT_ACTIONS:
            raise ValueError("unsupported runtime fault category")
        now = self.clock.now().isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id,details_json FROM continuous_demo_run_events WHERE run_id=? AND event_type='runtime_alert_v1'",
                (self.state.run_id,),
            ).fetchall()
            match = next(
                (row for row in rows if json.loads(str(row["details_json"]))["code"] == code), None
            )
            payload: dict[str, Any] = (
                json.loads(str(match["details_json"]))
                if match
                else {
                    "code": code,
                    "first_seen_at": now,
                    "occurrences": 0,
                    "recoveries": 0,
                    "active": False,
                    "recovery_action": ALERT_ACTIONS[code],
                }
            )
            was_active = payload["active"]
            payload["active"] = active
            if active:
                payload["occurrences"] += 1
                payload["last_seen_at"] = now
            elif was_active:
                payload["recoveries"] += 1
                payload["recovered_at"] = now
                connection.execute(
                    "INSERT INTO continuous_demo_run_events(run_id,event_type,details_json,created_at) VALUES (?,'runtime_recovered_v1',?,?)",
                    (
                        self.state.run_id,
                        json.dumps({"code": code, "action": ALERT_ACTIONS[code]}),
                        now,
                    ),
                )
            if match:
                connection.execute(
                    "UPDATE continuous_demo_run_events SET details_json=?,created_at=? WHERE id=?",
                    (json.dumps(payload, sort_keys=True), now, match["id"]),
                )
            elif active:
                connection.execute(
                    "INSERT INTO continuous_demo_run_events(run_id,event_type,details_json,created_at) VALUES (?,'runtime_alert_v1',?,?)",
                    (self.state.run_id, json.dumps(payload, sort_keys=True), now),
                )


def read_runtime_status(path: Path, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Open an existing database read-only; never initialize, migrate or unfreeze it."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        run = connection.execute(
            "SELECT * FROM continuous_demo_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError("runtime run_id does not exist")
        rows = connection.execute(
            "SELECT event_type,details_json FROM continuous_demo_run_events WHERE run_id=? AND event_type IN ('runtime_observation_v1','runtime_alert_v1') ORDER BY id",
            (run_id,),
        ).fetchall()
        observations = [
            json.loads(row["details_json"])
            for row in rows
            if row["event_type"] == "runtime_observation_v1"
        ]
        observation = observations[-1] if observations else None
        checkpoint = connection.execute(
            "SELECT strategy_version,last_candle_open_time,state_hash,state_json,updated_at FROM strategy_runtime_states WHERE run_id=? ORDER BY updated_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        reconciliation = connection.execute(
            "SELECT * FROM continuous_reconciliations WHERE run_id=? ORDER BY completed_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        unknown = connection.execute(
            "SELECT COUNT(*) FROM orders WHERE run_id=? AND state='unknown'", (run_id,)
        ).fetchone()[0]
        lock = connection.execute(
            "SELECT lock_name,lease_expires_at,released_at FROM continuous_run_locks WHERE run_id=? ORDER BY acquired_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if observation is not None and (
        type(observation.get("format_version")) is not int
        or observation.get("format_version") != 1
        or observation.get("run_id") != run_id
    ):
        raise ValueError("unsupported runtime observation format")
    health: dict[str, Any] = (
        assess(observation, now or datetime.now(UTC))
        if observation
        else {
            "health": "unknown",
            "process_heartbeat_fresh": None,
            "market_arriving": None,
            "business_progressing": None,
            "blocking_reasons": ["runtime_evidence_missing"],
            "recovery_actions": ["inspect_legacy_run_before_restart"],
        }
    )
    alerts = [
        json.loads(row["details_json"]) for row in rows if row["event_type"] == "runtime_alert_v1"
    ]
    if checkpoint and (
        hashlib.sha256(str(checkpoint["state_json"]).encode()).hexdigest()
        != str(checkpoint["state_hash"])
        or (observation and checkpoint["strategy_version"] != observation.get("strategy_version"))
    ):
        health["health"] = "blocked"
        health["blocking_reasons"].append("checkpoint_invalid")
        health["recovery_actions"].append(ALERT_ACTIONS["checkpoint_invalid"])
    if str(run["status"]) in {"failed", "frozen", "interrupted"}:
        health["health"] = "blocked"
        health["blocking_reasons"].append(str(run["stop_reason"] or run["status"]))
    for alert in alerts:
        if alert["active"]:
            health["health"] = "blocked"
            if alert["code"] not in health["blocking_reasons"]:
                health["blocking_reasons"].append(alert["code"])
                health["recovery_actions"].append(alert["recovery_action"])
    if unknown:
        health["health"] = "blocked"
        health["blocking_reasons"].append("unknown_order")
        health["recovery_actions"].append(ALERT_ACTIONS["unknown_order"])
    if reconciliation is not None and str(reconciliation["status"]) != "healthy":
        health["health"] = "blocked"
        health["blocking_reasons"].append("account_untrusted")
        health["recovery_actions"].append(ALERT_ACTIONS["account_untrusted"])
    account_trusted: bool | None = None
    if observation and observation.get("mode") == "shadow_with_private_checks":
        checked_at = (
            datetime.fromisoformat(str(reconciliation["completed_at"])) if reconciliation else None
        )
        account_trusted = bool(
            health["process_heartbeat_fresh"]
            and observation.get("private_connection") == "connected"
            and reconciliation
            and str(reconciliation["status"]) == "healthy"
            and checked_at is not None
            and 0 <= ((now or datetime.now(UTC)) - checked_at).total_seconds() <= 60
        )
        if not account_trusted:
            health["health"] = "blocked"
            if "account_untrusted" not in health["blocking_reasons"]:
                health["blocking_reasons"].append("account_untrusted")
                health["recovery_actions"].append(ALERT_ACTIONS["account_untrusted"])
    return {
        "format_version": 1,
        "run_id": run_id,
        "mode": run["mode"],
        "strategy_name": run["strategy_name"],
        "lifecycle": observation.get("lifecycle", "unknown") if observation else "unknown",
        "legacy_run_status": run["status"],
        "strategy_version": observation.get("strategy_version", "unknown")
        if observation
        else "unknown",
        "heartbeat_at": observation.get("heartbeat_at") if observation else None,
        "last_market_received_at": observation.get("market_received_at") if observation else None,
        "last_confirmed_processed_at": observation.get("confirmed_processed_at")
        if observation
        else None,
        "public_connection": observation.get("public_connection", "unknown")
        if observation
        else "unknown",
        "private_connection": observation.get("private_connection", "unknown")
        if observation
        else "unknown",
        "public_reconnects": observation.get("public_reconnects") if observation else None,
        "private_reconnects": observation.get("private_reconnects") if observation else None,
        "stop_reason": run["stop_reason"]
        or (observation.get("stop_reason") if observation else None),
        "observation": observation,
        "last_persisted_checkpoint": {
            key: value for key, value in dict(checkpoint).items() if key != "state_json"
        }
        if checkpoint
        else None,
        "lease": dict(lock) if lock else None,
        "latest_reconciliation": dict(reconciliation) if reconciliation else None,
        "reconciliation_status": str(reconciliation["status"]) if reconciliation else "unknown",
        "account_trusted": account_trusted,
        "alerts": alerts,
        **health,
        "trading_permission": False,
    }
