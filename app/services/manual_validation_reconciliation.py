"""Immutable recovery of one manually authorised Demo fill.

This service intentionally does not call OKX.  Its input must be captured from a
private read-only response before it is invoked, which makes the database write
small, deterministic and straightforward to audit.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.domain.order import OrderSide
from app.storage.database import Database


class ManualValidationReconciliationError(RuntimeError):
    """The supplied evidence cannot safely correct local historical state."""


@dataclass(frozen=True, slots=True)
class ExchangeManualValidationFill:
    order_id: str
    client_order_id: str
    trade_id: str
    instrument_id: str
    side: OrderSide
    fill_size: Decimal
    fill_price: Decimal
    fee: Decimal
    fee_currency: str
    filled_at: datetime
    source_response_hash: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    batch_id: str
    local_fill_created: bool
    scope_correction_created: bool
    inventory_created_or_reconciled: bool
    net_base_delta: Decimal
    gross_quote_spent: Decimal
    quote_fee: Decimal
    net_quote_delta: Decimal


class ManualValidationOrderReconciliationService:
    """Reconcile an exchange-confirmed manual validation fill without rewriting history."""

    _PROPOSAL_ENTITY = "demo_order_proposal"
    _REASON = "historical_manual_validation_misclassification"

    def __init__(self, database: Database, *, now: datetime | None = None) -> None:
        self.database = database
        self.now = (now or datetime.now(UTC)).astimezone(UTC)

    def effective_proposal_scope(self, proposal_id: str) -> str:
        with self.database.connect() as connection:
            proposal = connection.execute(
                "SELECT inventory_scope FROM demo_order_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ManualValidationReconciliationError("local proposal is missing")
            correction = connection.execute(
                """SELECT corrected_scope FROM inventory_scope_corrections
                   WHERE entity_type=? AND entity_id=? AND reason_code=?""",
                (self._PROPOSAL_ENTITY, proposal_id, self._REASON),
            ).fetchone()
        return str(correction[0]) if correction else str(proposal[0])

    def reconcile(
        self,
        fill: ExchangeManualValidationFill,
        *,
        evidence_confirms_manual_validation: bool,
        remaining_inventory_confirmed: bool,
        created_by: str = "phase_6c3b2",
    ) -> ReconciliationResult:
        self._validate_fill(fill)
        if not evidence_confirms_manual_validation:
            raise ManualValidationReconciliationError("inventory scope is unresolved")
        if fill.side is not OrderSide.BUY:
            raise ManualValidationReconciliationError(
                "manual validation recovery only accepts a buy fill"
            )

        net_base_delta, gross_quote_spent, quote_fee, net_quote_delta = self._deltas(fill)
        now = self.now.isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                """SELECT batch_id FROM reconciliation_batches
                   WHERE kind=? AND source_evidence_hash=?""",
                ("manual_validation_order_reconciliation", fill.source_response_hash),
            ).fetchone()
            batch_id = str(batch["batch_id"]) if batch is not None else uuid4().hex
            order = connection.execute(
                """SELECT * FROM orders WHERE client_order_id=? AND exchange_order_id=?""",
                (fill.client_order_id, fill.order_id),
            ).fetchone()
            if order is None:
                raise ManualValidationReconciliationError(
                    "local order does not exactly match exchange identifiers"
                )
            if (
                str(order["instrument_id"]) != fill.instrument_id
                or str(order["side"]) != fill.side.value
            ):
                raise ManualValidationReconciliationError(
                    "local order does not match exchange fill"
                )
            proposal = connection.execute(
                """SELECT * FROM demo_order_proposals
                   WHERE client_order_id=? AND exchange_order_id=?""",
                (fill.client_order_id, fill.order_id),
            ).fetchone()
            if proposal is None:
                raise ManualValidationReconciliationError("local proposal is missing")
            if (
                str(proposal["source"]) != "manual_demo_test"
                or str(order["order_source"]) != "manual_demo_test"
            ):
                raise ManualValidationReconciliationError(
                    "local evidence does not prove manual validation"
                )
            if str(proposal["inventory_scope"]) != "strategy_managed":
                raise ManualValidationReconciliationError("unexpected original proposal scope")

            if batch is None:
                connection.execute(
                    """INSERT INTO reconciliation_batches
                       (batch_id,kind,source_evidence_hash,created_at,created_by,schema_version)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        batch_id,
                        "manual_validation_order_reconciliation",
                        fill.source_response_hash,
                        now,
                        created_by,
                        1,
                    ),
                )
            existing_fill = connection.execute(
                "SELECT * FROM fills WHERE exchange_fill_id=?", (fill.trade_id,)
            ).fetchone()
            local_fill_created = False
            if existing_fill is None:
                connection.execute(
                    """INSERT INTO fills
                       (client_order_id,side,quantity,price,fee,filled_at,fill_id,exchange_fill_id,
                        fee_currency,data_quality_status,quarantine_reason,eligible_for_cost_basis,
                        source,mode,run_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fill.client_order_id,
                        fill.side.value,
                        str(fill.fill_size),
                        str(fill.fill_price),
                        str(fill.fee),
                        fill.filled_at.isoformat(),
                        self._fill_id(fill),
                        fill.trade_id,
                        fill.fee_currency,
                        "trusted",
                        None,
                        0,
                        "exchange_reconciliation",
                        str(order["mode"]),
                        str(order["run_id"]),
                    ),
                )
                local_fill_created = True
            else:
                self._assert_same_fill(existing_fill, fill)

            existing_correction = connection.execute(
                """SELECT original_scope,corrected_scope,supporting_order_id,supporting_trade_id,
                          source_evidence_hash
                   FROM inventory_scope_corrections
                   WHERE entity_type=? AND entity_id=? AND reason_code=?""",
                (self._PROPOSAL_ENTITY, str(proposal["proposal_id"]), self._REASON),
            ).fetchone()
            scope_correction_created = False
            expected_correction = (
                "strategy_managed",
                "manual_validation",
                fill.order_id,
                fill.trade_id,
                fill.source_response_hash,
            )
            if existing_correction is None:
                connection.execute(
                    """INSERT INTO inventory_scope_corrections
                       (correction_id,batch_id,entity_type,entity_id,original_scope,corrected_scope,reason_code,
                        supporting_order_id,supporting_trade_id,source_evidence_hash,created_at,created_by,schema_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid4().hex,
                        batch_id,
                        self._PROPOSAL_ENTITY,
                        str(proposal["proposal_id"]),
                        *expected_correction[:2],
                        self._REASON,
                        *expected_correction[2:],
                        now,
                        created_by,
                        1,
                    ),
                )
                scope_correction_created = True
            elif tuple(str(item) for item in existing_correction) != expected_correction:
                raise ManualValidationReconciliationError("conflicting inventory scope correction")

            inventory_created_or_reconciled = False
            if remaining_inventory_confirmed:
                self._assert_no_later_consumption(connection, str(order["run_id"]), fill.filled_at)
                inventory_created_or_reconciled = self._reconcile_inventory(
                    connection,
                    batch_id,
                    order,
                    fill,
                    net_base_delta,
                    gross_quote_spent,
                    quote_fee,
                    net_quote_delta,
                    now,
                )
        return ReconciliationResult(
            batch_id,
            local_fill_created,
            scope_correction_created,
            inventory_created_or_reconciled,
            net_base_delta,
            gross_quote_spent,
            quote_fee,
            net_quote_delta,
        )

    @staticmethod
    def _validate_fill(fill: ExchangeManualValidationFill) -> None:
        if not all(
            (
                fill.order_id,
                fill.client_order_id,
                fill.trade_id,
                fill.instrument_id,
                fill.fee_currency,
            )
        ):
            raise ManualValidationReconciliationError(
                "exchange fill identifiers and fee currency are required"
            )
        if len(fill.source_response_hash) != 64 or any(
            char not in "0123456789abcdef" for char in fill.source_response_hash
        ):
            raise ManualValidationReconciliationError(
                "source response hash must be a SHA256 digest"
            )
        if fill.fill_size <= 0 or fill.fill_price <= 0:
            raise ManualValidationReconciliationError("fill size and price must be positive")
        if fill.filled_at.tzinfo is None or fill.filled_at.utcoffset() is None:
            raise ManualValidationReconciliationError("fill time must be timezone-aware UTC")

    @staticmethod
    def _deltas(fill: ExchangeManualValidationFill) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        gross_quote_spent = fill.fill_size * fill.fill_price
        base_fee = (
            fill.fee if fill.fee_currency == fill.instrument_id.split("-", 1)[0] else Decimal("0")
        )
        quote_fee = (
            fill.fee if fill.fee_currency == fill.instrument_id.split("-", 1)[1] else Decimal("0")
        )
        return (
            fill.fill_size + base_fee,
            gross_quote_spent,
            quote_fee,
            -(gross_quote_spent - quote_fee),
        )

    @staticmethod
    def _fill_id(fill: ExchangeManualValidationFill) -> str:
        identity = ":".join(
            (
                fill.client_order_id,
                fill.trade_id,
                str(fill.fill_size),
                str(fill.fill_price),
                str(fill.fee),
                fill.filled_at.isoformat(),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _assert_same_fill(existing: sqlite3.Row, fill: ExchangeManualValidationFill) -> None:
        if (
            str(existing["client_order_id"]) != fill.client_order_id
            or str(existing["side"]) != fill.side.value
            or Decimal(str(existing["quantity"])) != fill.fill_size
            or Decimal(str(existing["price"])) != fill.fill_price
            or Decimal(str(existing["fee"])) != fill.fee
            or str(existing["fee_currency"]) != fill.fee_currency
            or datetime.fromisoformat(str(existing["filled_at"])).astimezone(UTC)
            != fill.filled_at.astimezone(UTC)
        ):
            raise ManualValidationReconciliationError(
                "existing trade id conflicts with exchange fill"
            )

    @staticmethod
    def _assert_no_later_consumption(
        connection: sqlite3.Connection, run_id: str, filled_at: datetime
    ) -> None:
        row = connection.execute(
            """SELECT 1 FROM fills AS f JOIN orders AS o ON o.client_order_id=f.client_order_id
               WHERE o.run_id=? AND o.side='sell' AND f.filled_at>? LIMIT 1""",
            (run_id, filled_at.isoformat()),
        ).fetchone()
        if row is not None:
            raise ManualValidationReconciliationError(
                "manual validation inventory has later consumption"
            )

    @staticmethod
    def _reconcile_inventory(
        connection: sqlite3.Connection,
        batch_id: str,
        order: sqlite3.Row,
        fill: ExchangeManualValidationFill,
        net_base_delta: Decimal,
        gross_quote_spent: Decimal,
        quote_fee: Decimal,
        net_quote_delta: Decimal,
        now: str,
    ) -> bool:
        strategy_name = "manual_validation"
        run_id, instrument_id = str(order["run_id"]), str(order["instrument_id"])
        row = connection.execute(
            """SELECT acquired_quantity,disposed_quantity,reserved_quantity FROM managed_inventory
               WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='manual_validation'""",
            (strategy_name, run_id, instrument_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO managed_inventory
                   (strategy_name,run_id,instrument_id,inventory_scope,acquired_quantity,disposed_quantity,
                    reserved_quantity,average_cost,realized_pnl,created_at,updated_at)
                   VALUES (?,?,?,?,?,'0','0',?,'0',?,?)""",
                (
                    strategy_name,
                    run_id,
                    instrument_id,
                    "manual_validation",
                    str(net_base_delta),
                    str(fill.fill_price),
                    now,
                    now,
                ),
            )
        else:
            available = (
                Decimal(str(row["acquired_quantity"]))
                - Decimal(str(row["disposed_quantity"]))
                - Decimal(str(row["reserved_quantity"]))
            )
            if available != net_base_delta:
                raise ManualValidationReconciliationError(
                    "manual validation inventory differs from confirmed fill"
                )
            return False
        connection.execute(
            """INSERT INTO inventory_reconciliation_events
               (event_id,batch_id,strategy_name,run_id,instrument_id,inventory_scope,event_type,gross_base_filled,
                base_fee,net_base_delta,gross_quote_spent,quote_fee,net_quote_delta,source_order_id,source_trade_id,
                created_at,source_evidence_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid4().hex,
                batch_id,
                strategy_name,
                run_id,
                instrument_id,
                "manual_validation",
                "manual_validation_fill_reconciled",
                str(fill.fill_size),
                str(
                    fill.fee
                    if fill.fee_currency == instrument_id.split("-", 1)[0]
                    else Decimal("0")
                ),
                str(net_base_delta),
                str(gross_quote_spent),
                str(quote_fee),
                str(net_quote_delta),
                fill.order_id,
                fill.trade_id,
                now,
                fill.source_response_hash,
            ),
        )
        return True
