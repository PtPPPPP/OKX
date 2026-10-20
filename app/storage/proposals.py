from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.domain.market import InstrumentType, TradeMode
from app.domain.order import (
    Order,
    OrderRequest,
    OrderSide,
    OrderSource,
    OrderState,
    OrderType,
)
from app.domain.private_state import PrivateStateSnapshot, PrivateStateStatus
from app.services.demo_order_preflight import DemoOrderProposal, ProposalStatus
from app.storage._codec import _decimal_text, _json, _now
from app.storage.database import Database


class PrivateStateFenceDeferred(RuntimeError):
    """A proposal remains safe but cannot enter submission while state is reconciling."""


class ProposalRepository:
    """Proposal lifecycle, one-use authorization and cross-table submission transactions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def save_demo_order_proposal(self, proposal: DemoOrderProposal) -> None:
        """Persist a proposal before any possible exchange operation."""
        with self.database.connect() as connection:
            state = connection.execute(
                "SELECT epoch,version FROM private_state_control WHERE control_id=1"
            ).fetchone()
            if state is None:
                raise RuntimeError("private state control is missing")
            connection.execute(
                """INSERT INTO demo_order_proposals (
                proposal_id, proposal_version, run_id, source, strategy_name,
                instrument_id, instrument_type, trade_mode, side, order_type,
                planned_limit_price, requested_notional, approved_notional, quantity, estimated_fee,
                instrument_rule_snapshot_id, account_snapshot_id, reconciliation_snapshot_id,
                capability_audit_id, risk_decision_id, client_order_id, proposal_hash, status,
                blockers_json, warnings_json, created_at, expires_at, restart_revalidation_required,
                submission_performed, private_state_epoch, private_state_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
                (
                    proposal.proposal_id,
                    proposal.proposal_version,
                    proposal.run_id,
                    proposal.source,
                    proposal.strategy_name,
                    proposal.instrument_id,
                    proposal.instrument_type.value,
                    proposal.trade_mode.value,
                    proposal.side.value,
                    proposal.order_type.value,
                    str(proposal.planned_limit_price),
                    str(proposal.requested_notional),
                    str(proposal.approved_notional),
                    str(proposal.quantity),
                    str(proposal.estimated_fee),
                    proposal.instrument_rule_snapshot_id,
                    proposal.account_snapshot_id,
                    proposal.reconciliation_snapshot_id,
                    proposal.capability_audit_id,
                    proposal.risk_decision_id,
                    proposal.client_order_id,
                    proposal.proposal_hash,
                    proposal.status.value,
                    _json({"items": proposal.blockers}),
                    _json({"items": proposal.warnings}),
                    proposal.created_at.isoformat(),
                    proposal.expires_at.isoformat(),
                    int(state["epoch"]),
                    int(state["version"]),
                ),
            )
            self._save_proposal_event(
                connection,
                proposal.proposal_id,
                "prepared",
                None,
                proposal.status.value,
                "proposal_created",
                {},
            )
            if proposal.status is ProposalStatus.BLOCKED:
                self._save_proposal_event(
                    connection,
                    proposal.proposal_id,
                    "blocked",
                    "prepared",
                    "blocked",
                    "preflight_blocked",
                    {"blockers": proposal.blockers},
                )
            connection.execute(
                "UPDATE demo_order_proposals SET signal_id=?, candle_id=?, acceptance_only=?, inventory_scope=?, submission_sequence=? WHERE proposal_id=?",
                (
                    proposal.signal_id,
                    proposal.candle_id,
                    int(proposal.acceptance_only),
                    proposal.inventory_scope,
                    proposal.submission_sequence,
                    proposal.proposal_id,
                ),
            )

    def load_demo_order_proposal(self, proposal_id: str) -> DemoOrderProposal | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM demo_order_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            return None
        blockers = tuple(json.loads(str(row["blockers_json"])).get("items", []))
        warnings = tuple(json.loads(str(row["warnings_json"])).get("items", []))
        return DemoOrderProposal(
            proposal_id=str(row["proposal_id"]),
            proposal_version=int(row["proposal_version"]),
            run_id=str(row["run_id"]),
            instrument_id=str(row["instrument_id"]),
            trade_mode=TradeMode(str(row["trade_mode"])),
            side=OrderSide(str(row["side"])),
            planned_limit_price=Decimal(str(row["planned_limit_price"])),
            quantity=Decimal(str(row["quantity"])),
            approved_notional=Decimal(str(row["approved_notional"])),
            estimated_fee=Decimal(str(row["estimated_fee"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            status=ProposalStatus(str(row["status"])),
            blockers=blockers,
            warnings=warnings,
            submission_performed=bool(row["submission_performed"]),
            proposal_hash=str(row["proposal_hash"]),
            source=str(row["source"]),
            strategy_name=str(row["strategy_name"]),
            instrument_type=InstrumentType(str(row["instrument_type"])),
            order_type=OrderType(str(row["order_type"])),
            requested_notional=Decimal(str(row["requested_notional"])),
            client_order_id=str(row["client_order_id"]),
            instrument_rule_snapshot_id=str(row["instrument_rule_snapshot_id"]),
            account_snapshot_id=str(row["account_snapshot_id"]),
            reconciliation_snapshot_id=str(row["reconciliation_snapshot_id"]),
            capability_audit_id=str(row["capability_audit_id"]),
            risk_decision_id=str(row["risk_decision_id"]),
            signal_id=str(row["signal_id"] or ""),
            candle_id=str(row["candle_id"] or ""),
            acceptance_only=bool(row["acceptance_only"]),
            inventory_scope=str(row["inventory_scope"] or "strategy_managed"),
            submission_sequence=int(row["submission_sequence"] or 0),
            private_state_epoch=int(row["private_state_epoch"]),
            private_state_version=int(row["private_state_version"]),
        )

    def load_submission_started_at(self, proposal_id: str) -> datetime | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT event_time FROM demo_order_proposal_events WHERE proposal_id=? AND event_type='submission_started' ORDER BY event_id LIMIT 1",
                (proposal_id,),
            ).fetchone()
        return datetime.fromisoformat(str(row[0])) if row else None

    def count_controlled_demo_submissions(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM demo_order_proposals WHERE submission_performed=1 AND status != 'operationally_not_created'"
            ).fetchone()
        return int(row[0])

    def close_unknown_order_operationally_not_created(
        self, proposal_id: str, *, reason: str
    ) -> None:
        """Close only with an explicit operator decision; preserve the complete audit trail."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT client_order_id,status,submission_performed FROM demo_order_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "unknown"
                or not bool(row["submission_performed"])
            ):
                raise ValueError("proposal is not an unknown submitted order")
            changed = connection.execute(
                "UPDATE demo_order_proposals SET status='operationally_not_created', invalidation_reason=? WHERE proposal_id=? AND status='unknown'",
                (reason, proposal_id),
            ).rowcount
            if changed != 1:
                raise ValueError("proposal status changed concurrently")
            connection.execute(
                "UPDATE orders SET state='rejected', updated_at=? WHERE client_order_id=? AND state='unknown'",
                (_now(), str(row["client_order_id"])),
            )
            self._save_proposal_event(
                connection,
                proposal_id,
                "operationally_closed",
                "unknown",
                "operationally_not_created",
                reason,
                {"operator_override": True, "submission_performed_preserved": True},
            )

    def resolve_unknown_order_from_authoritative_read(
        self,
        proposal_id: str,
        authoritative: Order,
    ) -> None:
        """Apply one authoritative read without making the private state immediately healthy."""
        if (
            authoritative.state in {OrderState.CREATED, OrderState.SUBMITTED, OrderState.UNKNOWN}
            or not authoritative.exchange_order_id
        ):
            raise ValueError("authoritative recovery order is not conclusive")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT p.client_order_id,p.instrument_id,p.status,p.submission_performed,
                o.state AS order_state
                FROM demo_order_proposals p
                JOIN orders o ON o.client_order_id=p.client_order_id
                WHERE p.proposal_id=?""",
                (proposal_id,),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "unknown"
                or not bool(row["submission_performed"])
                or str(row["order_state"]) != "unknown"
                or authoritative.request.client_order_id != str(row["client_order_id"])
                or authoritative.request.instrument_id != str(row["instrument_id"])
            ):
                raise ValueError("proposal is not the matching unknown submitted order")
            order_changed = connection.execute(
                """UPDATE orders SET exchange_order_id=?,state=?,filled_quantity=?,
                average_price=?,updated_at=? WHERE client_order_id=? AND state='unknown'""",
                (
                    authoritative.exchange_order_id,
                    authoritative.state.value,
                    str(authoritative.filled_quantity),
                    _decimal_text(authoritative.average_price),
                    (authoritative.updated_at or datetime.now(UTC)).isoformat(),
                    str(row["client_order_id"]),
                ),
            ).rowcount
            proposal_changed = connection.execute(
                """UPDATE demo_order_proposals SET status='submitted',exchange_order_id=?
                WHERE proposal_id=? AND status='unknown'""",
                (authoritative.exchange_order_id, proposal_id),
            ).rowcount
            if order_changed != 1 or proposal_changed != 1:
                raise RuntimeError("unknown order recovery changed concurrently")
            remaining = int(
                connection.execute("SELECT COUNT(*) FROM orders WHERE state='unknown'").fetchone()[
                    0
                ]
            )
            connection.execute(
                """UPDATE private_state_control
                SET status=?,unknown_order_count=?,version=version+1,
                dirty_reasons_json=?,updated_at=? WHERE control_id=1""",
                (
                    "reconciling_expected" if remaining == 0 else "frozen",
                    remaining,
                    _json(
                        {
                            "items": [
                                "authoritative_unknown_recovery_requires_reconciliation"
                                if remaining == 0
                                else "unresolved_unknown_submission"
                            ]
                        }
                    ),
                    _now(),
                ),
            )
            self._save_proposal_event(
                connection,
                proposal_id,
                "unknown_order_recovered",
                "unknown",
                "submitted",
                "authoritative_read_applied",
                {
                    "exchange_order_id": authoritative.exchange_order_id,
                    "order_state": authoritative.state.value,
                    "remaining_unknown_order_count": remaining,
                },
            )

    def save_unknown_recovery(self, result: Any) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO unknown_order_recoveries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result.recovery_id,
                    result.proposal_id,
                    result.local_order_id,
                    result.original_client_order_id,
                    result.recovery_status,
                    result.confidence,
                    result.exchange_order_id,
                    _json({"items": result.blockers}),
                    _json({"items": result.warnings}),
                    result.created_at.isoformat(),
                    result.created_at.isoformat(),
                ),
            )
            for q in result.queries:
                connection.execute(
                    "INSERT INTO unknown_order_recovery_queries (recovery_id,endpoint,begin_at,end_at,pages_read,records_read,completed,http_status,okx_code,error_classification,error_message,contract_status,applicability_status,blocking,superseded_by,first_record_time,last_record_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.recovery_id,
                        q.endpoint,
                        q.begin.isoformat() if q.begin else None,
                        q.end.isoformat() if q.end else None,
                        q.pages_read,
                        q.records_read,
                        int(q.completed),
                        q.http_status,
                        q.okx_code,
                        q.error_classification,
                        q.error_message,
                        q.contract_status,
                        q.applicability_status,
                        int(q.blocking),
                        q.superseded_by,
                        q.first_record_time.isoformat() if q.first_record_time else None,
                        q.last_record_time.isoformat() if q.last_record_time else None,
                    ),
                )

    def load_unknown_recoveries(self, proposal_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM unknown_order_recoveries WHERE proposal_id=? ORDER BY created_at",
                (proposal_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def transition_demo_order_proposal(
        self,
        proposal_id: str,
        *,
        expected: ProposalStatus,
        new: ProposalStatus,
        event_type: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        allowed = {
            ProposalStatus.PREPARED: {
                ProposalStatus.READY_FOR_CONFIRMATION,
                ProposalStatus.BLOCKED,
                ProposalStatus.EXPIRED,
                ProposalStatus.INVALIDATED,
            },
            ProposalStatus.READY_FOR_CONFIRMATION: {
                ProposalStatus.SUBMISSION_IN_PROGRESS,
                ProposalStatus.EXPIRED,
                ProposalStatus.INVALIDATED,
            },
            ProposalStatus.SUBMISSION_IN_PROGRESS: {
                ProposalStatus.SUBMITTED,
                ProposalStatus.UNKNOWN,
                ProposalStatus.INVALIDATED,
            },
            ProposalStatus.SUBMITTED: {ProposalStatus.CONSUMED},
            ProposalStatus.UNKNOWN: {ProposalStatus.SUBMITTED, ProposalStatus.INVALIDATED},
        }
        if new not in allowed.get(expected, set()):
            raise ValueError(f"illegal proposal transition: {expected.value} -> {new.value}")
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE demo_order_proposals SET status=?, invalidated_at=CASE WHEN ?='invalidated' THEN ? ELSE invalidated_at END, invalidation_reason=CASE WHEN ?='invalidated' THEN ? ELSE invalidation_reason END WHERE proposal_id=? AND status=?",
                (new.value, new.value, _now(), new.value, reason, proposal_id, expected.value),
            ).rowcount
            if changed != 1:
                raise ValueError("proposal status changed concurrently or proposal does not exist")
            self._save_proposal_event(
                connection,
                proposal_id,
                event_type,
                expected.value,
                new.value,
                reason,
                details or {},
            )

    def save_revalidation_event(self, proposal_id: str, event_type: str, reason: str) -> None:
        """Append a proposal revalidation lifecycle event (public, append-only)."""
        with self.database.connect() as connection:
            self._save_proposal_event(connection, proposal_id, event_type, None, None, reason, {})

    def defer_fenced_demo_order_proposal(self, proposal_id: str, reason: str) -> None:
        """Keep a proposal unconsumed when the state changed after its fence."""
        with self.database.connect() as connection:
            self._save_proposal_event(
                connection,
                proposal_id,
                "submission_deferred",
                "ready_for_confirmation",
                "ready_for_confirmation",
                reason,
                {},
            )

    def begin_controlled_demo_submission(
        self, proposal: DemoOrderProposal, *, maximum_submissions: int = 1
    ) -> Order:
        """Atomically consume the only stage slot and create the local parent order."""
        if proposal.status is not ProposalStatus.READY_FOR_CONFIRMATION:
            raise ValueError("proposal is not ready for submission")
        now = _now()
        request = OrderRequest(
            proposal.client_order_id,
            proposal.instrument_id,
            proposal.side,
            proposal.order_type,
            proposal.quantity,
            proposal.planned_limit_price,
            proposal.proposal_id,
            datetime.fromisoformat(now),
            run_id=proposal.run_id,
            strategy_name=proposal.strategy_name,
            mode="demo",
            order_source=OrderSource.MANUAL_DEMO_TEST,
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = connection.execute(
                "SELECT version,status,unknown_order_count FROM private_state_control WHERE control_id=1"
            ).fetchone()
            fence = connection.execute(
                "SELECT fenced_private_state_version FROM demo_order_proposals WHERE proposal_id=?",
                (proposal.proposal_id,),
            ).fetchone()
            if control is None or fence is None:
                raise RuntimeError("private state submission fence is missing")
            if (
                str(control["status"]) != "healthy"
                or int(control["unknown_order_count"]) != 0
                or fence["fenced_private_state_version"] is None
                or int(fence["fenced_private_state_version"]) != int(control["version"])
            ):
                raise PrivateStateFenceDeferred("private_state_changed_after_submission_fence")
            used = connection.execute(
                "SELECT COUNT(*) FROM demo_order_proposals WHERE run_id=? AND submission_performed=1 AND status != 'operationally_not_created'",
                (proposal.run_id,),
            ).fetchone()[0]
            in_progress = connection.execute(
                "SELECT COUNT(*) FROM demo_order_proposals WHERE run_id=? AND status='submission_in_progress'",
                (proposal.run_id,),
            ).fetchone()[0]
            if int(used) >= maximum_submissions:
                raise ValueError("bounded demo submission budget exhausted")
            if int(in_progress) != 0:
                raise ValueError("bounded demo submission already in progress")
            changed = connection.execute(
                """UPDATE demo_order_proposals SET status='submission_in_progress',
                submission_performed=1, submitted_at=? WHERE proposal_id=?
                AND status='ready_for_confirmation' AND submission_performed=0""",
                (now, proposal.proposal_id),
            ).rowcount
            if changed != 1:
                raise ValueError("proposal was concurrently changed")
            connection.execute(
                """INSERT INTO orders(client_order_id,exchange_order_id,instrument_id,side,order_type,
                quantity,price,signal_id,state,filled_quantity,average_price,created_at,updated_at,
                run_id,mode,strategy_name,bar,order_source)
                VALUES (?,NULL,?,?,?,?,?,?, 'created','0',NULL,?,?,?,?,?,?,?)""",
                (
                    request.client_order_id,
                    request.instrument_id,
                    request.side.value,
                    request.order_type.value,
                    str(request.quantity),
                    str(request.price),
                    request.signal_id,
                    now,
                    now,
                    request.run_id,
                    request.mode,
                    request.strategy_name,
                    request.bar,
                    request.order_source.value,
                ),
            )
            self._save_proposal_event(
                connection,
                proposal.proposal_id,
                "submission_started",
                "ready_for_confirmation",
                "submission_in_progress",
                "local_transaction_committed",
                {},
            )
            connection.execute(
                "INSERT INTO bounded_submission_events(run_id,proposal_id,slot_number,event_type,details_json,created_at) VALUES (?,?,?,?,?,?)",
                (proposal.run_id, proposal.proposal_id, int(used) + 1, "slot_reserved", "{}", now),
            )
        return Order(request=request, updated_at=datetime.fromisoformat(now))

    def authorize_controlled_demo_write(
        self,
        proposal_id: str,
        client_order_id: str,
        *,
        operation: str,
    ) -> int:
        """Atomically reserve the only low-level write capability for a controlled order."""
        if operation not in {"place", "cancel"}:
            raise ValueError("unsupported controlled Demo write operation")
        event_type = f"{operation}_write_authorized"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT
                    p.status AS proposal_status,
                    p.submission_performed,
                    p.instrument_id AS proposal_instrument_id,
                    p.instrument_type,
                    p.trade_mode,
                    p.order_type AS proposal_order_type,
                    p.quantity AS proposal_quantity,
                    p.planned_limit_price,
                    p.approved_notional,
                    p.client_order_id AS proposal_client_order_id,
                    p.fenced_private_state_version,
                    o.instrument_id AS order_instrument_id,
                    o.order_type AS order_order_type,
                    o.quantity AS order_quantity,
                    o.price AS order_price,
                    o.signal_id,
                    o.state AS order_state,
                    o.mode AS order_mode,
                    o.order_source,
                    c.version AS private_state_version,
                    c.status AS private_state_status,
                    c.unknown_order_count
                FROM demo_order_proposals p
                JOIN orders o ON o.client_order_id=p.client_order_id
                JOIN private_state_control c ON c.control_id=1
                WHERE p.proposal_id=? AND p.client_order_id=?""",
                (proposal_id, client_order_id),
            ).fetchone()
            if row is None:
                raise PermissionError("controlled Demo order linkage is missing")
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM private_state_snapshots WHERE needs_reconciliation=1"
                ).fetchone()[0]
            )
            already_issued = connection.execute(
                """SELECT 1 FROM demo_order_proposal_events
                WHERE proposal_id=? AND event_type=? LIMIT 1""",
                (proposal_id, event_type),
            ).fetchone()
            if already_issued is not None:
                raise PermissionError("controlled Demo write authorization was already issued")
            private_state_version = int(row["private_state_version"])
            common_valid = (
                int(row["submission_performed"]) == 1
                and str(row["proposal_instrument_id"]) == "BTC-USDT"
                and str(row["proposal_instrument_id"]) == str(row["order_instrument_id"])
                and str(row["instrument_type"]) == "spot"
                and str(row["trade_mode"]) == "cash"
                and str(row["proposal_order_type"]) == "limit"
                and str(row["order_order_type"]) == "limit"
                and str(row["proposal_client_order_id"]) == client_order_id
                and str(row["signal_id"]) == proposal_id
                and str(row["order_mode"]) == "demo"
                and str(row["order_source"]) == "manual_demo_test"
                and Decimal(str(row["proposal_quantity"])) == Decimal(str(row["order_quantity"]))
                and Decimal(str(row["planned_limit_price"])) == Decimal(str(row["order_price"]))
                and Decimal("0") < Decimal(str(row["approved_notional"])) <= Decimal("5")
                and Decimal(str(row["proposal_quantity"]))
                * Decimal(str(row["planned_limit_price"]))
                == Decimal(str(row["approved_notional"]))
                and str(row["private_state_status"]) == "healthy"
                and int(row["unknown_order_count"]) == 0
                and pending == 0
            )
            if operation == "place":
                operation_valid = (
                    str(row["proposal_status"]) == "submission_in_progress"
                    and str(row["order_state"]) == "created"
                    and row["fenced_private_state_version"] is not None
                    and int(row["fenced_private_state_version"]) == private_state_version
                )
            else:
                operation_valid = str(row["proposal_status"]) == "submitted" and str(
                    row["order_state"]
                ) in {"submitted", "accepted", "partially_filled"}
            if not common_valid or not operation_valid:
                raise PermissionError("controlled Demo write fence rejected the request")
            self._save_proposal_event(
                connection,
                proposal_id,
                event_type,
                str(row["proposal_status"]),
                str(row["proposal_status"]),
                "one_use_authorization_reserved",
                {
                    "client_order_id": client_order_id,
                    "operation": operation,
                    "private_state_version": private_state_version,
                },
            )
        return private_state_version

    def complete_controlled_demo_submission(
        self, proposal_id: str, order: Order, *, event_type: str, proposal_status: ProposalStatus
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE orders SET exchange_order_id=?, state=?, filled_quantity=?, average_price=?,
                updated_at=? WHERE client_order_id=?""",
                (
                    order.exchange_order_id,
                    order.state.value,
                    str(order.filled_quantity),
                    _decimal_text(order.average_price),
                    (order.updated_at or datetime.now(UTC)).isoformat(),
                    order.request.client_order_id,
                ),
            )
            connection.execute(
                "UPDATE demo_order_proposals SET status=?, exchange_order_id=? WHERE proposal_id=?",
                (proposal_status.value, order.exchange_order_id, proposal_id),
            )
            self._save_proposal_event(
                connection,
                proposal_id,
                event_type,
                "submission_in_progress",
                proposal_status.value,
                "exchange_response_recorded",
                {},
            )

    def mark_controlled_demo_submission_unknown(
        self, proposal_id: str, *, error_category: str, http_status: int | None
    ) -> None:
        """Atomically freeze an ambiguous result before any read-only recovery."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT client_order_id,status FROM demo_order_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("proposal not found")
            if str(row["status"]) not in {"submission_in_progress", "unknown"}:
                raise ValueError("proposal is not an ambiguous submission")
            connection.execute(
                "UPDATE orders SET state='unknown', updated_at=? WHERE client_order_id=? AND state IN ('created','submitted')",
                (_now(), str(row["client_order_id"])),
            )
            connection.execute(
                "UPDATE demo_order_proposals SET status='unknown', exchange_order_id=NULL WHERE proposal_id=?",
                (proposal_id,),
            )
            if str(row["status"]) != "unknown":
                self._save_proposal_event(
                    connection,
                    proposal_id,
                    "submission_unknown",
                    "submission_in_progress",
                    "unknown",
                    "ambiguous_exchange_response",
                    {"category": error_category, "http_status": http_status},
                )
            unknown_count = connection.execute(
                "SELECT COUNT(*) FROM orders WHERE state='unknown'"
            ).fetchone()[0]
            connection.execute(
                """UPDATE private_state_control
                SET status='frozen',unknown_order_count=?,dirty_reasons_json=?,updated_at=?
                WHERE control_id=1""",
                (
                    int(unknown_count),
                    _json({"items": ["unknown_submission"]}),
                    _now(),
                ),
            )

    @staticmethod
    def _save_proposal_event(
        connection: Any,
        proposal_id: str,
        event_type: str,
        previous: str | None,
        new: str | None,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO demo_order_proposal_events(proposal_id,event_type,event_time,previous_status,new_status,reason,details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, event_type, _now(), previous, new, reason, _json(details)),
        )

    def fence_demo_order_proposal(self, proposal_id: str) -> PrivateStateSnapshot:
        """Atomically bind a validated proposal to the current healthy private state."""
        now = _now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                "SELECT status,fenced_private_state_version FROM demo_order_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            control = connection.execute(
                "SELECT * FROM private_state_control WHERE control_id=1"
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) FROM private_state_snapshots WHERE needs_reconciliation=1"
            ).fetchone()[0]
            if proposal is None or str(proposal["status"]) != "ready_for_confirmation":
                raise ValueError("proposal is not ready for submission fence")
            if proposal["fenced_private_state_version"] is not None:
                raise ValueError("proposal has already entered the submission fence")
            if control is None:
                raise RuntimeError("private state control is missing")
            snapshot = self._private_snapshot_from_row(control)
            if not snapshot.submission_allowed or int(pending) != 0:
                raise PrivateStateFenceDeferred(
                    "proposal_deferred_during_expected_reconciliation"
                    if snapshot.status is PrivateStateStatus.RECONCILING_EXPECTED
                    else f"proposal_blocked_by_private_state:{snapshot.status.value}"
                )
            connection.execute(
                """UPDATE demo_order_proposals
                SET fenced_private_state_version=?,fenced_at=? WHERE proposal_id=?""",
                (snapshot.version, now, proposal_id),
            )
            self._save_proposal_event(
                connection,
                proposal_id,
                "submission_fenced",
                "ready_for_confirmation",
                "ready_for_confirmation",
                "private_state_token_compatible",
                {"private_state_epoch": snapshot.epoch, "private_state_version": snapshot.version},
            )
        return snapshot

    @staticmethod
    def _private_snapshot_from_row(row: Any) -> PrivateStateSnapshot:
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
