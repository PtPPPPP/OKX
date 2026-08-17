"""Evidence-cutoff based classification for legacy runs without a terminal timestamp."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.storage.database import Database

SAFE_CLASSIFICATION = "R2_ACCOUNT_ACTIVITY_FULLY_EXCLUDED_THROUGH_CUTOFF"
INSUFFICIENT_COVERAGE = "R6"


@dataclass(frozen=True, slots=True)
class EvidenceCoverageWindow:
    run_id: str
    run_start_at: datetime
    evidence_cutoff_at: datetime
    query_completed_at: datetime
    orders_coverage_complete: bool
    fills_coverage_complete: bool
    pagination_complete: bool
    retention_covers_window: bool
    current_open_orders: int
    current_positions: int
    unknown_orders: int
    owner_process_present: bool
    fresh_heartbeat_present: bool
    lock_live: bool
    new_events_after_snapshot: bool
    snapshot_hash: str
    limitations: tuple[str, ...]
    historical_terminal_time: datetime | None = None
    future_drift_monitor_required: bool = True

    @property
    def classification(self) -> str:
        if all(
            (
                self.orders_coverage_complete,
                self.fills_coverage_complete,
                self.pagination_complete,
                self.retention_covers_window,
                self.current_open_orders == 0,
                self.current_positions == 0,
                self.unknown_orders == 0,
                not self.owner_process_present,
                not self.fresh_heartbeat_present,
                not self.lock_live,
                not self.new_events_after_snapshot,
                self.future_drift_monitor_required,
            )
        ):
            return SAFE_CLASSIFICATION
        return INSUFFICIENT_COVERAGE

    def as_record(self) -> dict[str, object]:
        start = self.run_start_at.astimezone(UTC)
        cutoff = self.evidence_cutoff_at.astimezone(UTC)
        if cutoff < start:
            raise ValueError("evidence cutoff precedes run start")
        terminal = (
            self.historical_terminal_time.astimezone(UTC) if self.historical_terminal_time else None
        )
        if terminal is not None and terminal < start:
            raise ValueError("historical terminal time precedes run start")
        limitations = list(self.limitations)
        if terminal is None:
            limitations.append("historical_terminal_time_unknown")
        if not self.retention_covers_window:
            limitations.append("endpoint_retention_limit")
        return {
            "run_id": self.run_id,
            "run_start_at": start.isoformat(),
            "historical_terminal_time": terminal.isoformat() if terminal else None,
            "historical_terminal_time_known": int(terminal is not None),
            "evidence_cutoff_at": cutoff.isoformat(),
            "coverage_start_at": start.isoformat(),
            "coverage_end_at": cutoff.isoformat(),
            "coverage_end_source": "audit_evidence_cutoff",
            "query_completed_at": self.query_completed_at.astimezone(UTC).isoformat(),
            "orders_coverage": "complete" if self.orders_coverage_complete else "incomplete",
            "fills_coverage": "complete" if self.fills_coverage_complete else "incomplete",
            "current_open_order_check": str(self.current_open_orders),
            "current_position_check": str(self.current_positions),
            "process_absence_check": "absent" if not self.owner_process_present else "present",
            "heartbeat_check": "missing_unexpected"
            if not self.fresh_heartbeat_present
            else "fresh",
            "lock_check": "expired_or_absent" if not self.lock_live else "live",
            "snapshot_hash": self.snapshot_hash,
            "limitations": json.dumps(sorted(set(limitations)), separators=(",", ":")),
        }


class EvidenceCoverageWindowRepository:
    """Persist a cutoff assessment idempotently; it never changes a legacy run."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, window: EvidenceCoverageWindow) -> str:
        record = window.as_record()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM evidence_coverage_windows WHERE run_id=?", (window.run_id,)
            ).fetchone()
            if existing is not None:
                if {key: existing[key] for key in record} != record:
                    raise ValueError(
                        "evidence coverage window conflicts with existing immutable record"
                    )
                return window.classification
            columns = ",".join(record)
            placeholders = ",".join("?" for _ in record)
            connection.execute(
                f"INSERT INTO evidence_coverage_windows ({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )
        return window.classification
