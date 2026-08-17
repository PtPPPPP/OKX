from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.order import OrderSide
from app.services.manual_validation_reconciliation import (
    ExchangeManualValidationFill,
    ManualValidationOrderReconciliationService,
    ManualValidationReconciliationError,
)
from app.storage.database import Database

ORDER_ID = "3764864043753639936"
CLIENT_ORDER_ID = "D0e4e1ff1cebf4dc194559f5343eb2df"
TRADE_ID = "1330248846"
PROPOSAL_ID = "0e4e1ff1cebf4dc194559f5343eb2dfb"
RUN_ID = "manual-validation-run"
NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'reconciliation.db'}")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO orders
               (client_order_id,exchange_order_id,instrument_id,side,order_type,quantity,price,signal_id,state,
                filled_quantity,average_price,created_at,updated_at,run_id,mode,strategy_name,bar,order_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                CLIENT_ORDER_ID,
                ORDER_ID,
                "BTC-USDT",
                "buy",
                "limit",
                "0.00007586",
                "65908.2",
                PROPOSAL_ID,
                "filled",
                "0.00007586",
                "65908.2",
                NOW.isoformat(),
                NOW.isoformat(),
                RUN_ID,
                "demo",
                "moving_average_cross",
                "",
                "manual_demo_test",
            ),
        )
        connection.execute(
            """INSERT INTO demo_order_proposals
               (proposal_id,proposal_version,run_id,source,strategy_name,instrument_id,instrument_type,trade_mode,
                side,order_type,planned_limit_price,requested_notional,approved_notional,quantity,estimated_fee,
                instrument_rule_snapshot_id,account_snapshot_id,reconciliation_snapshot_id,capability_audit_id,
                risk_decision_id,client_order_id,proposal_hash,status,blockers_json,warnings_json,created_at,expires_at,
                exchange_order_id,signal_id,candle_id,acceptance_only,inventory_scope,submission_sequence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                PROPOSAL_ID,
                1,
                RUN_ID,
                "manual_demo_test",
                "moving_average_cross",
                "BTC-USDT",
                "spot",
                "cash",
                "buy",
                "limit",
                "65908.2",
                "5",
                "4.999796052",
                "0.00007586",
                "0.004999796052",
                "instrument",
                "account",
                "reconciliation",
                "capability",
                "risk",
                CLIENT_ORDER_ID,
                "hash",
                "submitted",
                "{}",
                "{}",
                NOW.isoformat(),
                NOW.isoformat(),
                ORDER_ID,
                "",
                "",
                0,
                "strategy_managed",
                0,
            ),
        )
    return database


def _fill(
    *, fee_currency: str = "BTC", fee: str = "-0.000000060688"
) -> ExchangeManualValidationFill:
    return ExchangeManualValidationFill(
        order_id=ORDER_ID,
        client_order_id=CLIENT_ORDER_ID,
        trade_id=TRADE_ID,
        instrument_id="BTC-USDT",
        side=OrderSide.BUY,
        fill_size=Decimal("0.00007586"),
        fill_price=Decimal("65908.2"),
        fee=Decimal(fee),
        fee_currency=fee_currency,
        filled_at=datetime(2026, 7, 22, 7, 8, 33, 580000, tzinfo=UTC),
        source_response_hash="0a48e590e60eb8f04a84e45740927266ff3de0ab4a25210767f8ce69f3ed7802",
    )


def test_recovery_is_idempotent_and_keeps_original_proposal_scope(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = ManualValidationOrderReconciliationService(database, now=NOW)

    first = service.reconcile(
        _fill(), evidence_confirms_manual_validation=True, remaining_inventory_confirmed=True
    )
    second = service.reconcile(
        _fill(), evidence_confirms_manual_validation=True, remaining_inventory_confirmed=True
    )

    assert first.local_fill_created is True
    assert first.scope_correction_created is True
    assert first.inventory_created_or_reconciled is True
    assert first.net_base_delta == Decimal("0.000075799312")
    assert second.batch_id == first.batch_id
    assert second.local_fill_created is False
    assert second.scope_correction_created is False
    assert second.inventory_created_or_reconciled is False
    assert service.effective_proposal_scope(PROPOSAL_ID) == "manual_validation"
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT inventory_scope FROM demo_order_proposals WHERE proposal_id=?",
                (PROPOSAL_ID,),
            ).fetchone()[0]
            == "strategy_managed"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fills WHERE exchange_fill_id=?", (TRADE_ID,)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM inventory_reconciliation_events WHERE source_trade_id=?",
                (TRADE_ID,),
            ).fetchone()[0]
            == 1
        )
        inventory = connection.execute(
            "SELECT acquired_quantity FROM managed_inventory WHERE inventory_scope='manual_validation'"
        ).fetchone()
        assert inventory[0] == "0.000075799312"


def test_recovery_rejects_mismatched_order_and_unresolved_scope(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = ManualValidationOrderReconciliationService(database, now=NOW)
    invalid = replace(_fill(), order_id="different")

    with pytest.raises(ManualValidationReconciliationError, match="identifiers"):
        service.reconcile(
            invalid, evidence_confirms_manual_validation=True, remaining_inventory_confirmed=False
        )
    with pytest.raises(ManualValidationReconciliationError, match="unresolved"):
        service.reconcile(
            _fill(), evidence_confirms_manual_validation=False, remaining_inventory_confirmed=False
        )


def test_quote_fee_does_not_change_base_inventory(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = ManualValidationOrderReconciliationService(database, now=NOW)

    result = service.reconcile(
        _fill(fee_currency="USDT", fee="-0.005"),
        evidence_confirms_manual_validation=True,
        remaining_inventory_confirmed=True,
    )

    assert result.net_base_delta == Decimal("0.00007586")
    assert result.quote_fee == Decimal("-0.005")
    assert result.net_quote_delta == Decimal("-5.004796052")
