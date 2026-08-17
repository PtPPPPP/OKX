from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.domain.account import PrivateAccountState
from app.domain.market import Instrument, InstrumentType, TradeMode
from app.domain.order import (
    Order,
    OrderRequest,
    OrderSide,
    OrderSource,
    OrderState,
    OrderType,
)
from app.domain.position import (
    AccountConfiguration,
    AccountEquitySnapshot,
    AccountMode,
    AssetBalance,
    BalanceSource,
    BalanceValidationStatus,
    PortfolioSnapshot,
    PositionCost,
)
from app.domain.private_state import PrivateStateSnapshot, PrivateStateStatus
from app.domain.risk import RiskDecision
from app.domain.signal import Signal
from app.portfolio.cost_basis import CostFill
from app.services.demo_order_preflight import DemoOrderProposal, ProposalStatus
from app.storage.database import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


class PrivateStateFenceDeferred(RuntimeError):
    """A proposal remains safe but cannot enter submission while state is reconciling."""


class TradingRepository:
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

    def initialize_inventory_scope(
        self,
        *,
        strategy_name: str,
        run_id: str,
        instrument_id: str,
        scope: str,
        quantity: Decimal,
        average_cost: Decimal | None,
    ) -> None:
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO managed_inventory (strategy_name,run_id,instrument_id,inventory_scope,acquired_quantity,disposed_quantity,reserved_quantity,average_cost,realized_pnl,created_at,updated_at) VALUES (?,?,?,?,?,'0','0',?,'0',?,?)",
                (
                    strategy_name,
                    run_id,
                    instrument_id,
                    scope,
                    str(quantity),
                    _decimal_text(average_cost),
                    now,
                    now,
                ),
            )

    def managed_strategy_quantity(
        self, strategy_name: str, run_id: str, instrument_id: str
    ) -> Decimal:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT acquired_quantity,disposed_quantity,reserved_quantity FROM managed_inventory WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (strategy_name, run_id, instrument_id),
            ).fetchone()
        return (
            Decimal("0")
            if row is None
            else Decimal(str(row[0])) - Decimal(str(row[1])) - Decimal(str(row[2]))
        )

    def managed_strategy_position(
        self, strategy_name: str, run_id: str, instrument_id: str
    ) -> tuple[Decimal, Decimal | None]:
        """Return only inventory owned by this strategy run, never account inventory."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT acquired_quantity,disposed_quantity,reserved_quantity,average_cost FROM managed_inventory WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (strategy_name, run_id, instrument_id),
            ).fetchone()
        if row is None:
            return Decimal("0"), None
        quantity = Decimal(str(row[0])) - Decimal(str(row[1])) - Decimal(str(row[2]))
        return quantity, Decimal(str(row[3])) if row[3] is not None else None

    def managed_strategy_quantity_for_generation(
        self, strategy_name: str, run_id: str, instrument_id: str, generation_id: str
    ) -> Decimal:
        quantity, _ = self.managed_strategy_position_for_generation(
            strategy_name, run_id, instrument_id, generation_id
        )
        return quantity

    def managed_strategy_position_for_generation(
        self, strategy_name: str, run_id: str, instrument_id: str, generation_id: str
    ) -> tuple[Decimal, Decimal | None]:
        """Return inventory only when its source run belongs to the requested generation."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT mi.acquired_quantity,mi.disposed_quantity,mi.reserved_quantity,mi.average_cost
                FROM managed_inventory AS mi
                JOIN continuous_demo_runs AS run ON run.run_id=mi.run_id
                WHERE mi.strategy_name=? AND mi.run_id=? AND mi.instrument_id=?
                  AND mi.inventory_scope='strategy_managed' AND run.generation_id=?""",
                (strategy_name, run_id, instrument_id, generation_id),
            ).fetchone()
        if row is None:
            return Decimal("0"), None
        quantity = Decimal(str(row[0])) - Decimal(str(row[1])) - Decimal(str(row[2]))
        return quantity, Decimal(str(row[3])) if row[3] is not None else None

    def apply_managed_fill(
        self,
        *,
        strategy_name: str,
        run_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
    ) -> None:
        if quantity <= 0 or price <= 0:
            raise ValueError("managed fill quantity and price must be positive")
        now = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT acquired_quantity,disposed_quantity,reserved_quantity,average_cost,realized_pnl FROM managed_inventory WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (strategy_name, run_id, instrument_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO managed_inventory(strategy_name,run_id,instrument_id,inventory_scope,acquired_quantity,disposed_quantity,reserved_quantity,average_cost,realized_pnl,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        strategy_name,
                        run_id,
                        instrument_id,
                        "strategy_managed",
                        "0",
                        "0",
                        "0",
                        None,
                        "0",
                        now,
                        now,
                    ),
                )
                acquired = disposed = reserved = Decimal("0")
                average = None
                realized = Decimal("0")
            else:
                acquired = Decimal(str(row[0]))
                disposed = Decimal(str(row[1]))
                reserved = Decimal(str(row[2]))
                average = Decimal(str(row[3])) if row[3] is not None else None
                realized = Decimal(str(row[4]))
            if side is OrderSide.BUY:
                current = acquired - disposed
                total_cost = (average or Decimal("0")) * current + price * quantity + fee
                acquired += quantity
                average = total_cost / (current + quantity)
            else:
                available = acquired - disposed - reserved
                if quantity > available:
                    raise ValueError("sell fill exceeds strategy-managed inventory")
                disposed += quantity
                realized += (price - (average or price)) * quantity - fee
            connection.execute(
                "UPDATE managed_inventory SET acquired_quantity=?,disposed_quantity=?,reserved_quantity=?,average_cost=?,realized_pnl=?,updated_at=? WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (
                    str(acquired),
                    str(disposed),
                    str(reserved),
                    str(average) if average is not None else None,
                    str(realized),
                    now,
                    strategy_name,
                    run_id,
                    instrument_id,
                ),
            )

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

    def save_order(self, order: Order) -> None:
        updated_at = (order.updated_at or order.request.created_at).isoformat()
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (order.request.client_order_id,),
            ).fetchone()
            if current is not None and _state_regresses(
                OrderState(str(current["state"])), order.state
            ):
                raise ValueError(f"拒绝订单状态回退: {current['state']} -> {order.state.value}")
            connection.execute(
                """INSERT INTO orders
                (client_order_id, exchange_order_id, instrument_id, side, order_type,
                 quantity, price, signal_id, state, filled_quantity, average_price,
                 created_at, updated_at, run_id, mode, strategy_name, bar, order_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                  exchange_order_id=excluded.exchange_order_id,
                  state=excluded.state, filled_quantity=excluded.filled_quantity,
                  average_price=excluded.average_price, updated_at=excluded.updated_at""",
                (
                    order.request.client_order_id,
                    order.exchange_order_id,
                    order.request.instrument_id,
                    order.request.side.value,
                    order.request.order_type.value,
                    str(order.request.quantity),
                    str(order.request.price),
                    order.request.signal_id,
                    order.state.value,
                    str(order.filled_quantity),
                    str(order.average_price) if order.average_price is not None else None,
                    order.request.created_at.isoformat(),
                    updated_at,
                    order.request.run_id,
                    order.request.mode,
                    order.request.strategy_name,
                    order.request.bar,
                    order.request.order_source.value,
                ),
            )
            audit_run_id = str(current["run_id"]) if current is not None else order.request.run_id
            audit_mode = str(current["mode"]) if current is not None else order.request.mode
            audit_strategy = (
                str(current["strategy_name"])
                if current is not None
                else order.request.strategy_name
            )
            audit_bar = str(current["bar"]) if current is not None else order.request.bar
            audit_signal_id = (
                str(current["signal_id"]) if current is not None else order.request.signal_id
            )
            audit_source = (
                str(current["order_source"])
                if current is not None
                else order.request.order_source.value
            )
            connection.execute(
                """INSERT OR IGNORE INTO order_state_changes
                (client_order_id, state, changed_at, run_id, mode, strategy_name,
                 instrument_id, bar, signal_id, exchange_order_id, order_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.request.client_order_id,
                    order.state.value,
                    updated_at,
                    audit_run_id,
                    audit_mode,
                    audit_strategy,
                    order.request.instrument_id,
                    audit_bar,
                    audit_signal_id,
                    order.exchange_order_id or "",
                    audit_source,
                ),
            )

    def load_order(self, client_order_id: str) -> Order | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def load_open_orders(self) -> list[Order]:
        terminal = tuple(
            state.value
            for state in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            )
        )
        placeholders = ",".join("?" for _ in terminal)
        with self.database.connect() as connection:
            query = (
                f"SELECT * FROM orders WHERE state NOT IN ({placeholders}) ORDER BY updated_at DESC"
            )
            rows = connection.execute(query, terminal).fetchall()
        return [self._row_to_order(row) for row in rows]

    def load_recent_order_times(self, since: datetime) -> tuple[datetime, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT created_at FROM orders WHERE created_at >= ? ORDER BY created_at",
                (since.isoformat(),),
            ).fetchall()
        return tuple(datetime.fromisoformat(str(row["created_at"])) for row in rows)

    def load_cost_fills(self, instrument_id: str) -> list[CostFill]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT f.side, f.quantity, f.price, f.fee, f.fee_currency,
                          f.filled_at
                FROM fills AS f
                JOIN orders AS o ON o.client_order_id = f.client_order_id
                WHERE o.instrument_id = ? AND o.mode = 'demo'
                  AND f.eligible_for_cost_basis = 1 AND f.mode = 'demo'
                ORDER BY f.filled_at""",
                (instrument_id,),
            ).fetchall()
        return [
            CostFill(
                side=OrderSide(str(row["side"])),
                quantity=Decimal(str(row["quantity"])),
                price=Decimal(str(row["price"])),
                fee=Decimal(str(row["fee"])),
                fee_currency=(str(row["fee_currency"]) if row["fee_currency"] else None),
                filled_at=datetime.fromisoformat(str(row["filled_at"])),
            )
            for row in rows
        ]

    def save_fill(
        self,
        client_order_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        filled_at: datetime,
        *,
        fill_id: str | None = None,
        exchange_fill_id: str | None = None,
        fee_currency: str | None = None,
        run_id: str | None = None,
        mode: str | None = None,
    ) -> bool:
        stable_fill_id = fill_id or canonical_fill_id(
            client_order_id, side, quantity, price, fee, filled_at
        )
        with self.database.connect() as connection:
            parent = connection.execute(
                """SELECT mode, run_id FROM orders WHERE client_order_id = ?""",
                (client_order_id,),
            ).fetchone()
            has_parent = parent is not None or (run_id is not None and mode is not None)
            mode = str(parent["mode"]) if parent else str(mode or "")
            run_id = str(parent["run_id"]) if parent else str(run_id or "")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO fills
                (client_order_id, side, quantity, price, fee, filled_at,
                 fill_id, exchange_fill_id, fee_currency, data_quality_status,
                 quarantine_reason, eligible_for_cost_basis, source, mode, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    client_order_id,
                    side.value,
                    str(quantity),
                    str(price),
                    str(fee),
                    filled_at.isoformat(),
                    stable_fill_id,
                    exchange_fill_id,
                    fee_currency,
                    "trusted" if has_parent else "quarantined",
                    None if has_parent else "missing_parent_order",
                    int(has_parent and mode == "demo"),
                    "private_event" if has_parent else "orphan_event",
                    mode,
                    run_id,
                ),
            )
        return cursor.rowcount == 1

    def save_portfolio_snapshot(
        self,
        portfolio: PortfolioSnapshot,
        instrument: Instrument,
        mark_price: Decimal,
        captured_at: datetime,
        *,
        run_id: str,
        mode: str,
        strategy_name: str,
        bar: str,
    ) -> None:
        quote_balance = portfolio.cash_balance(instrument.quote_currency) or Decimal("0")
        base_quantity = portfolio.position(instrument.instrument_id)
        managed_equity = quote_balance + base_quantity * mark_price
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO portfolio_snapshots
                (run_id, mode, strategy_name, instrument_id, bar, balances_json,
                 positions_json, average_entry_prices_json, equity, managed_equity, captured_at,
                 asset_balances_json, position_costs_json, balance_model_version,
                 field_contract_version, account_configuration_json, account_equity_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    mode,
                    strategy_name,
                    instrument.instrument_id,
                    bar,
                    _json(dict(portfolio.balances)),
                    _json(dict(portfolio.positions)),
                    _json(dict(portfolio.average_entry_prices)),
                    str(managed_equity),
                    str(managed_equity),
                    captured_at.isoformat(),
                    _json(
                        {
                            currency: {
                                "currency": asset.currency,
                                "cash_balance": _decimal_text(asset.cash_balance),
                                "available_balance": _decimal_text(asset.available_balance),
                                "frozen_balance": _decimal_text(asset.frozen_balance),
                                "equity": _decimal_text(asset.equity),
                                "equity_usd": _decimal_text(asset.equity_usd),
                                "discount_equity": _decimal_text(asset.discount_equity),
                                "liabilities": _decimal_text(asset.liabilities),
                                "unrealized_pnl": _decimal_text(asset.unrealized_pnl),
                                "holding_quantity": _decimal_text(asset.holding_quantity),
                                "spendable_quantity": _decimal_text(asset.spendable_quantity),
                                "raw_field_presence": sorted(asset.raw_field_presence),
                                "fetched_at": asset.fetched_at.isoformat(),
                                "account_mode": asset.account_mode.value,
                                "validation_status": asset.validation_status.value,
                            }
                            for currency, asset in portfolio.asset_balances.items()
                        }
                    ),
                    _json(
                        {
                            instrument_id: {
                                "average_entry_price": (
                                    str(cost.average_entry_price)
                                    if cost.average_entry_price is not None
                                    else None
                                ),
                                "cost_source": cost.cost_source,
                                "cost_is_reliable": cost.cost_is_reliable,
                            }
                            for instrument_id, cost in portfolio.position_costs.items()
                        }
                    ),
                    portfolio.model_version,
                    "okx-v5-2026-07",
                    _json(
                        {
                            "account_mode": portfolio.account_configuration.account_mode.value,
                            "position_mode": portfolio.account_configuration.position_mode,
                            "auto_loan_enabled": portfolio.account_configuration.auto_loan_enabled,
                            "greeks_type": portfolio.account_configuration.greeks_type,
                            "fetched_at": portfolio.account_configuration.fetched_at.isoformat(),
                        }
                        if portfolio.account_configuration
                        else {}
                    ),
                    _json(
                        {
                            "okx_total_equity": _decimal_text(
                                portfolio.account_equity.okx_total_equity
                            ),
                            "okx_adjusted_equity": _decimal_text(
                                portfolio.account_equity.okx_adjusted_equity
                            ),
                            "okx_equity_usd": _decimal_text(
                                portfolio.account_equity.okx_equity_usd
                            ),
                            "account_mode": portfolio.account_equity.account_mode.value,
                        }
                        if portfolio.account_equity
                        else {}
                    ),
                ),
            )

    def load_latest_portfolio_snapshot(
        self, instrument: Instrument
    ) -> tuple[PortfolioSnapshot, Decimal, datetime] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM portfolio_snapshots
                WHERE instrument_id = ?
                  AND balance_model_version IN ('v4', 'backtest-v1')
                ORDER BY captured_at DESC LIMIT 1""",
                (instrument.instrument_id,),
            ).fetchone()
        if row is None:
            return None
        balances = json.loads(str(row["balances_json"]))
        positions = json.loads(str(row["positions_json"]))
        average_entry_prices = json.loads(str(row["average_entry_prices_json"]))
        asset_balances = json.loads(str(row["asset_balances_json"]))
        position_costs = json.loads(str(row["position_costs_json"]))
        configuration_data = json.loads(str(row["account_configuration_json"]))
        account_equity_data = json.loads(str(row["account_equity_json"]))
        return (
            PortfolioSnapshot(
                balances={key: Decimal(str(value)) for key, value in balances.items()},
                positions={key: Decimal(str(value)) for key, value in positions.items()},
                average_entry_prices={
                    key: Decimal(str(value)) for key, value in average_entry_prices.items()
                },
                asset_balances={
                    key: AssetBalance(
                        currency=str(value["currency"]),
                        cash_balance=_optional_decimal(value.get("cash_balance")),
                        available_balance=_optional_decimal(value.get("available_balance")),
                        frozen_balance=_optional_decimal(value.get("frozen_balance")),
                        equity=_optional_decimal(value.get("equity")),
                        equity_usd=_optional_decimal(value.get("equity_usd")),
                        discount_equity=_optional_decimal(value.get("discount_equity")),
                        liabilities=_optional_decimal(value.get("liabilities")),
                        unrealized_pnl=_optional_decimal(value.get("unrealized_pnl")),
                        holding_quantity=_optional_decimal(value.get("holding_quantity")),
                        spendable_quantity=_optional_decimal(value.get("spendable_quantity")),
                        account_mode=AccountMode(str(value.get("account_mode") or "unknown")),
                        source=BalanceSource.REST,
                        fetched_at=datetime.fromisoformat(str(value["fetched_at"])),
                        raw_field_presence=frozenset(value.get("raw_field_presence") or ()),
                        is_authoritative=True,
                        validation_status=BalanceValidationStatus(
                            str(value.get("validation_status") or "insufficient_data")
                        ),
                    )
                    for key, value in asset_balances.items()
                },
                position_costs={
                    key: PositionCost(
                        average_entry_price=(
                            Decimal(str(value["average_entry_price"]))
                            if value.get("average_entry_price") is not None
                            else None
                        ),
                        cost_source=str(value["cost_source"]),
                        cost_is_reliable=bool(value["cost_is_reliable"]),
                    )
                    for key, value in position_costs.items()
                },
                account_configuration=(
                    AccountConfiguration(
                        AccountMode(str(configuration_data["account_mode"])),
                        configuration_data.get("position_mode"),
                        configuration_data.get("auto_loan_enabled"),
                        configuration_data.get("greeks_type"),
                        datetime.fromisoformat(
                            str(configuration_data.get("fetched_at") or row["captured_at"])
                        ),
                    )
                    if configuration_data.get("account_mode")
                    else None
                ),
                account_equity=(
                    AccountEquitySnapshot(
                        _optional_decimal(account_equity_data.get("okx_total_equity")),
                        _optional_decimal(account_equity_data.get("okx_adjusted_equity")),
                        _optional_decimal(account_equity_data.get("okx_equity_usd")),
                        AccountMode(str(account_equity_data["account_mode"])),
                    )
                    if account_equity_data.get("account_mode")
                    else None
                ),
                model_version=str(row["balance_model_version"]),
                trusted_for_trading=(
                    str(row["balance_model_version"]) == "v4"
                    and configuration_data.get("account_mode") == AccountMode.SPOT.value
                ),
            ),
            Decimal(str(row["managed_equity"])),
            datetime.fromisoformat(str(row["captured_at"])),
        )

    def daily_risk_metrics(
        self, instrument_id: str, now: datetime, current_equity: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT managed_equity FROM portfolio_snapshots
                WHERE instrument_id = ? AND mode = 'demo' AND captured_at >= ?
                ORDER BY captured_at""",
                (instrument_id, day_start.isoformat()),
            ).fetchall()
        if not rows:
            return None, None
        equities = [Decimal(str(row["managed_equity"])) for row in rows]
        first = equities[0]
        peak = max([current_equity, *equities])
        daily_pnl = current_equity - first
        drawdown = (peak - current_equity) / peak * Decimal("100") if peak > 0 else Decimal("0")
        return daily_pnl, drawdown

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

    @staticmethod
    def _row_to_order(row: Any) -> Order:
        request = OrderRequest(
            client_order_id=str(row["client_order_id"]),
            instrument_id=str(row["instrument_id"]),
            side=OrderSide(str(row["side"])),
            order_type=OrderType(str(row["order_type"])),
            quantity=Decimal(str(row["quantity"])),
            price=Decimal(str(row["price"])),
            signal_id=str(row["signal_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            run_id=str(row["run_id"]),
            strategy_name=str(row["strategy_name"]),
            mode=str(row["mode"]),
            bar=str(row["bar"]),
            order_source=OrderSource(str(row["order_source"])),
        )
        state = OrderState(str(row["state"]))
        return Order(
            request=request,
            state=state,
            exchange_order_id=str(row["exchange_order_id"]) if row["exchange_order_id"] else None,
            filled_quantity=Decimal(str(row["filled_quantity"])),
            average_price=Decimal(str(row["average_price"])) if row["average_price"] else None,
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            status_history=[state],
        )


def canonical_fill_id(
    client_order_id: str,
    side: OrderSide,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    filled_at: datetime,
) -> str:
    identity = ":".join(
        (
            client_order_id,
            side.value,
            str(quantity),
            str(price),
            str(fee),
            filled_at.isoformat(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _state_regresses(current: OrderState, incoming: OrderState) -> bool:
    if current is incoming:
        return False
    terminal = {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
    if current in terminal:
        return True
    if incoming is OrderState.UNKNOWN:
        return False
    if current is OrderState.UNKNOWN:
        return incoming in {OrderState.CREATED, OrderState.SUBMITTED}
    rank = {
        OrderState.CREATED: 0,
        OrderState.SUBMITTED: 1,
        OrderState.ACCEPTED: 2,
        OrderState.PARTIALLY_FILLED: 3,
        OrderState.CANCEL_PENDING: 3,
        OrderState.FILLED: 4,
        OrderState.CANCELLED: 4,
        OrderState.REJECTED: 4,
        OrderState.EXPIRED: 4,
    }
    return rank[incoming] < rank[current]
