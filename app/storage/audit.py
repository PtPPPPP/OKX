from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.events import DomainEvent
from app.domain.risk import RiskDecision
from app.domain.signal import Signal
from app.storage._codec import _json, _now
from app.storage.database import Database


class AuditRepository:
    """Independent audit, run provenance and event idempotency records."""

    def __init__(
        self, database: Database, *, event_mode: str = "unknown", event_bar: str = "unknown"
    ) -> None:
        self.database = database
        self.event_mode = event_mode
        self.event_bar = event_bar

    def append_event(self, event: DomainEvent) -> bool:
        """Commit the idempotency key AND complete event before publish returns."""
        identity = asdict(event)
        identity.pop("event_id")
        digest = hashlib.sha256(
            _json({"event": identity, "mode": self.event_mode, "bar": self.event_bar}).encode()
        ).hexdigest()
        key = event.idempotency_key or event.event_id
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_type,payload_hash FROM processed_events WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                if (str(row[0]), str(row[1])) != (event.event_type, digest):
                    raise ValueError("相同事件幂等键对应了不同负载或身份")
                return False
            connection.execute(
                "INSERT INTO processed_events(idempotency_key,event_type,payload_hash,processed_at) VALUES (?,?,?,?)",
                (key, event.event_type, digest, _now()),
            )
            connection.execute(
                "INSERT INTO audit_records(record_type,run_id,mode,strategy_name,instrument_id,bar,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "domain_event",
                    event.run_id,
                    self.event_mode,
                    event.strategy_name,
                    event.instrument_id,
                    self.event_bar,
                    json.dumps(asdict(event), sort_keys=True, default=str),
                    _now(),
                ),
            )
        return True

    def save_signal(self, signal: Signal) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO signals VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.signal_id,
                    signal.action.value,
                    signal.instrument_id,
                    signal.timestamp.isoformat(),
                    signal.reason,
                    str(signal.confidence),
                    _json(signal.metadata),
                ),
            )

    def save_audit_record(
        self,
        *,
        record_type: str,
        run_id: str,
        mode: str,
        strategy_name: str,
        instrument_id: str,
        bar: str,
        payload: dict[str, Any],
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO audit_records
                (record_type, run_id, mode, strategy_name, instrument_id, bar,
                 payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_type,
                    run_id,
                    mode,
                    strategy_name,
                    instrument_id,
                    bar,
                    _json(payload),
                    _now(),
                ),
            )

    def save_audit_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        created_at = _now()
        values = [
            (
                str(record["record_type"]),
                str(record["run_id"]),
                str(record["mode"]),
                str(record["strategy_name"]),
                str(record["instrument_id"]),
                str(record["bar"]),
                _json(dict(record["payload"])),
                created_at,
            )
            for record in records
        ]
        with self.database.connect() as connection:
            connection.executemany(
                """INSERT INTO audit_records
                (record_type, run_id, mode, strategy_name, instrument_id, bar,
                 payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )

    def save_candle_metadata(
        self,
        instrument_id: str,
        bar: str,
        first_timestamp: datetime,
        last_timestamp: datetime,
        row_count: int,
        source: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO candle_metadata
                (instrument_id, bar, first_timestamp, last_timestamp, row_count, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    instrument_id,
                    bar,
                    first_timestamp.isoformat(),
                    last_timestamp.isoformat(),
                    row_count,
                    source,
                    _now(),
                ),
            )

    def save_risk_decision(self, signal_id: str, decision: RiskDecision) -> None:
        adjusted = decision.adjusted_order
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO risk_decisions
                (signal_id, allowed, reason, adjusted_quantity, adjusted_notional,
                 snapshot_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_id,
                    int(decision.allowed),
                    "; ".join(decision.reasons),
                    str(adjusted.quantity if adjusted is not None else Decimal("0")),
                    str(adjusted.notional if adjusted is not None else Decimal("0")),
                    _json(decision.risk_snapshot),
                    _now(),
                ),
            )

    def save_runtime_mode(self, mode: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO runtime_state (key, value, updated_at) VALUES ('mode', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at""",
                (mode, _now()),
            )

    def load_runtime_mode(self) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key = 'mode'"
            ).fetchone()
        return str(row["value"]) if row else None

    def save_backtest_run(self, run_id: str, summary: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO backtest_runs VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    str(summary["started_at"]),
                    str(summary["completed_at"]),
                    str(summary["initial_capital"]),
                    str(summary["final_equity"]),
                    _json(summary),
                ),
            )

    def save_run_manifest(self, manifest: dict[str, Any], *, status: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO runs
                (run_id, mode, strategy_name, instrument_id, bar, status,
                 app_version, git_commit, git_dirty, config_hash, data_hash,
                 instrument_snapshot_hash, seed, started_at, completed_at,
                 candle_count, cost_parameters_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status=excluded.status, data_hash=excluded.data_hash,
                  completed_at=excluded.completed_at,
                  candle_count=excluded.candle_count""",
                (
                    str(manifest["run_id"]),
                    str(manifest.get("mode", "backtest")),
                    str(manifest["strategy_name"]),
                    str(manifest["instrument_id"]),
                    str(manifest["bar"]),
                    status,
                    str(manifest["app_version"]),
                    str(manifest["git_commit"]),
                    int(bool(manifest["git_dirty"])),
                    str(manifest["config_hash"]),
                    str(manifest["data_hash"]),
                    str(manifest["instrument_snapshot_hash"]),
                    int(manifest["seed"]),
                    str(manifest["started_at"]),
                    str(manifest.get("completed_at") or "") or None,
                    int(manifest["candle_count"]),
                    _json(dict(manifest["cost_parameters"])),
                ),
            )

    def save_instrument_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO instrument_snapshots
                (snapshot_hash, instrument_id, fetched_at, source, schema_version,
                 raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(snapshot["snapshot_hash"]),
                    str(dict(snapshot["raw"])["instrument_id"]),
                    str(snapshot["fetched_at"]),
                    str(snapshot["source"]),
                    int(snapshot["schema_version"]),
                    _json(dict(snapshot["raw"])),
                    _now(),
                ),
            )

    def save_dataset_snapshot(self, manifest: dict[str, Any]) -> None:
        if int(manifest["candle_count"]) <= 0:
            raise ValueError("空数据集不能保存为回测快照")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO dataset_snapshots
                (data_hash, instrument_id, bar, first_timestamp, last_timestamp,
                 candle_count, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(manifest["data_hash"]),
                    str(manifest["instrument_id"]),
                    str(manifest["bar"]),
                    str(manifest["data_started_at"]),
                    str(manifest["data_completed_at"]),
                    int(manifest["candle_count"]),
                    str(manifest.get("data_source", "configured")),
                    _now(),
                ),
            )

    def save_system_event(self, event_type: str, message: str, details: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO system_events
                (event_type, message, details_json, created_at) VALUES (?, ?, ?, ?)""",
                (event_type, message, _json(details), _now()),
            )

    def claim_event(self, idempotency_key: str, event_type: str, payload_hash: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO processed_events
                (idempotency_key, event_type, payload_hash, processed_at)
                VALUES (?, ?, ?, ?)""",
                (idempotency_key, event_type, payload_hash, _now()),
            )
            if cursor.rowcount == 1:
                return True
            existing = connection.execute(
                """SELECT event_type, payload_hash FROM processed_events
                WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
        if existing is None:
            raise RuntimeError("事件幂等写入结果不一致")
        if (
            str(existing["event_type"]) != event_type
            or str(existing["payload_hash"]) != payload_hash
        ):
            raise ValueError("相同事件幂等键对应了不同事件或负载")
        return False
