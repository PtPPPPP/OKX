from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from app.domain.account import PrivateAccountState
from app.domain.private_state import PrivateStateSnapshot, PrivateStateStatus
from app.storage._codec import _json, _now
from app.storage.database import Database


class PrivateStateRepository:
    """Coordinator-owned private state persistence and readiness."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def apply_private_state_event(
        self,
        idempotency_key: str,
        event_type: str,
        payload_hash: str,
        state: PrivateAccountState,
    ) -> bool:
        normalized = {
            "event_kind": state.event_kind,
            "event_time": state.event_time.isoformat(),
            "balances": {
                currency: {
                    "cash_balance": str(balance.cash_balance),
                    "available_balance": (
                        str(balance.available_balance)
                        if balance.available_balance is not None
                        else None
                    ),
                    "frozen_balance": (
                        str(balance.frozen_balance) if balance.frozen_balance is not None else None
                    ),
                    "equity": str(balance.equity) if balance.equity is not None else None,
                    "usd_equity": (
                        str(balance.usd_equity) if balance.usd_equity is not None else None
                    ),
                    "updated_at": balance.updated_at.isoformat(),
                }
                for currency, balance in state.balances.items()
            },
            "derivative_positions": {
                key: str(value) for key, value in state.derivative_positions.items()
            },
        }
        now = _now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO processed_events
                (idempotency_key, event_type, payload_hash, processed_at)
                VALUES (?, ?, ?, ?)""",
                (idempotency_key, event_type, payload_hash, now),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """SELECT event_type, payload_hash FROM processed_events
                    WHERE idempotency_key = ?""",
                    (idempotency_key,),
                ).fetchone()
                if existing is None or (
                    str(existing["event_type"]) != event_type
                    or str(existing["payload_hash"]) != payload_hash
                ):
                    raise ValueError("相同事件幂等键对应了不同事件或负载")
                return False
            existing_state = connection.execute(
                """SELECT event_time FROM private_state_snapshots
                WHERE scope_key = ?""",
                (state.scope_key,),
            ).fetchone()
            is_newer = existing_state is None or state.event_time >= datetime.fromisoformat(
                str(existing_state["event_time"])
            )
            if is_newer:
                connection.execute(
                    """INSERT INTO private_state_snapshots
                    (scope_key, event_kind, event_time, normalized_json, payload_hash,
                     needs_reconciliation, received_at, confirmed_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
                    ON CONFLICT(scope_key) DO UPDATE SET
                      event_kind=excluded.event_kind,
                      event_time=excluded.event_time,
                      normalized_json=excluded.normalized_json,
                      payload_hash=excluded.payload_hash,
                      needs_reconciliation=1,
                      received_at=excluded.received_at,
                      confirmed_at=NULL""",
                    (
                        state.scope_key,
                        state.event_kind,
                        state.event_time.isoformat(),
                        _json(normalized),
                        payload_hash,
                        now,
                    ),
                )
                connection.execute(
                    """UPDATE private_state_control
                    SET version=version+1,status='reconciling_expected',last_event_at=?,
                    dirty_reasons_json=?,updated_at=? WHERE control_id=1""",
                    (
                        state.event_time.isoformat(),
                        _json({"items": [f"private_event:{state.event_kind}"]}),
                        now,
                    ),
                )
            connection.execute(
                """INSERT INTO system_events
                (event_type, message, details_json, created_at) VALUES (?, ?, ?, ?)""",
                (
                    f"private_ws_{state.event_kind}",
                    "收到模拟盘私有暂态；等待 REST 对账"
                    if is_newer
                    else "忽略早于本地暂态的私有事件",
                    _json(
                        {
                            "scope_key": state.scope_key,
                            "event_time": state.event_time.isoformat(),
                            "currencies": sorted(state.balances),
                            "has_nonzero_derivative_position": (
                                state.has_nonzero_derivative_position
                            ),
                            "applied": is_newer,
                        }
                    ),
                    now,
                ),
            )
        return True

    def record_private_ws_watermark(
        self,
        *,
        connection_epoch: int,
        watermark: int,
        event_kind: str,
        event_at: datetime,
    ) -> None:
        """Persist only forward coordinator watermarks; stale input never becomes ready state."""
        with self.database.connect() as connection:
            control = connection.execute(
                "SELECT epoch,ws_watermark FROM private_state_control WHERE control_id=1"
            ).fetchone()
            if control is None:
                raise RuntimeError("private state control is missing")
            if int(control["epoch"]) != connection_epoch:
                raise ValueError("private connection epoch does not match control state")
            if watermark <= int(control["ws_watermark"]):
                raise ValueError("private websocket watermark did not advance")
            connection.execute(
                """UPDATE private_state_control
                SET ws_watermark=?,version=version+1,status='reconciling_expected',
                last_event_at=?,dirty_reasons_json=?,updated_at=? WHERE control_id=1""",
                (
                    watermark,
                    event_at.isoformat(),
                    _json({"items": [f"private_event:{event_kind}"]}),
                    _now(),
                ),
            )

    def begin_private_connection_epoch(self, connection_epoch: int) -> None:
        with self.database.connect() as connection:
            control = connection.execute(
                "SELECT epoch FROM private_state_control WHERE control_id=1"
            ).fetchone()
            if control is None:
                raise RuntimeError("private state control is missing")
            if connection_epoch <= int(control["epoch"]):
                raise ValueError("private connection epoch did not advance")
            connection.execute(
                """UPDATE private_state_control
                SET epoch=?,version=version+1,ws_watermark=0,status='reconciling_expected',
                dirty_reasons_json=?,updated_at=? WHERE control_id=1""",
                (
                    connection_epoch,
                    _json({"items": ["private_connection_epoch_changed"]}),
                    _now(),
                ),
            )

    def begin_private_reconciliation(self, reconciliation_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE private_state_control
                SET version=version+1,status='reconciling_expected',dirty_reasons_json=?,updated_at=?
                WHERE control_id=1""",
                (
                    _json({"items": [f"reconciliation:{reconciliation_id}"]}),
                    _now(),
                ),
            )

    def freeze_private_state(self, reason: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE private_state_control
                SET version=version+1,status='frozen',dirty_reasons_json=?,updated_at=?
                WHERE control_id=1""",
                (_json({"items": [reason]}), _now()),
            )

    def confirm_private_state_snapshots(self, confirmed_at: datetime) -> int:
        """Confirm only pushes received before the REST reconciliation watermark."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE private_state_snapshots
                SET needs_reconciliation = 0, confirmed_at = ?
                WHERE needs_reconciliation = 1 AND received_at <= ?""",
                (confirmed_at.isoformat(), confirmed_at.isoformat()),
            )
            pending = connection.execute(
                "SELECT COUNT(*) FROM private_state_snapshots WHERE needs_reconciliation=1"
            ).fetchone()[0]
            connection.execute(
                """UPDATE private_state_control
                SET version=version+1,status=?,last_consistent_at=CASE WHEN ?=0 THEN ? ELSE last_consistent_at END,
                dirty_reasons_json=?,updated_at=? WHERE control_id=1""",
                (
                    "healthy" if int(pending) == 0 else "reconciling_expected",
                    int(pending),
                    confirmed_at.isoformat(),
                    _json({"items": [] if int(pending) == 0 else ["private_event_pending"]}),
                    _now(),
                ),
            )
        return cursor.rowcount

    def has_unreconciled_private_state(self) -> bool:
        return not self.private_state_snapshot().submission_allowed

    def private_state_snapshot(self) -> PrivateStateSnapshot:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM private_state_control WHERE control_id=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("private state control is missing")
        reasons = tuple(json.loads(str(row["dirty_reasons_json"])).get("items", []))
        return PrivateStateSnapshot(
            epoch=int(row["epoch"]),
            version=int(row["version"]),
            ws_watermark=int(row["ws_watermark"]),
            status=PrivateStateStatus(str(row["status"])),
            last_consistent_at=(
                datetime.fromisoformat(str(row["last_consistent_at"]))
                if row["last_consistent_at"]
                else None
            ),
            last_event_at=(
                datetime.fromisoformat(str(row["last_event_at"])) if row["last_event_at"] else None
            ),
            dirty_reasons=reasons,
            unknown_order_count=int(row["unknown_order_count"]),
        )

    def has_nonzero_private_derivative_position(self) -> bool:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT normalized_json FROM private_state_snapshots
                WHERE event_kind = 'position'"""
            ).fetchall()
        for row in rows:
            payload = json.loads(str(row["normalized_json"]))
            positions = dict(payload.get("derivative_positions", {}))
            if any(Decimal(str(quantity)) != 0 for quantity in positions.values()):
                return True
        return False
