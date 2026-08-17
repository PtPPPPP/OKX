from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.order import Order, OrderRequest, OrderSide, OrderSource, OrderState, OrderType
from app.market.private_websocket import (
    OKXPrivateEventAdapter,
    PrivateEvent,
    PrivateEventKind,
)
from app.services.private_events import PrivateEventProcessor
from app.storage.database import Database
from app.storage.repositories import TradingRepository


def test_partial_fill_event_is_applied_once(tmp_path: Path) -> None:
    fixture = json.loads(Path("tests/fixtures/okx/contracts.json").read_text(encoding="utf-8"))
    event = OKXPrivateEventAdapter.parse(json.dumps(fixture["ws_order_partial"]))[0]
    path = tmp_path / "private-events.db"
    database = Database(f"sqlite:///{path}")
    database.initialize()
    processor = PrivateEventProcessor(TradingRepository(database))

    assert processor.process(event)
    assert not processor.process(event)

    with sqlite3.connect(path) as connection:
        fills = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        quantity = connection.execute("SELECT quantity FROM fills").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]
    assert fills == 1
    assert quantity == "0.0004"
    assert events == 1


def test_fill_keeps_controlled_run_identity(tmp_path: Path) -> None:
    fixture = json.loads(Path("tests/fixtures/okx/contracts.json").read_text(encoding="utf-8"))
    event = OKXPrivateEventAdapter.parse(json.dumps(fixture["ws_order_partial"]))[0]
    database = Database(f"sqlite:///{tmp_path / 'controlled-fill.db'}")
    database.initialize()
    repository = TradingRepository(database)
    now = datetime.now(UTC)
    request = OrderRequest(
        "client-1",
        "BTC-USDT",
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100"),
        "signal",
        now,
        run_id="bounded-run",
        strategy_name="moving_average_cross",
        mode="demo",
        order_source=OrderSource.STRATEGY_DEMO,
    )
    repository.save_order(Order(request, state=OrderState.ACCEPTED, updated_at=now))

    assert PrivateEventProcessor(repository).process(event)

    assert repository.managed_strategy_quantity(
        "moving_average_cross", "bounded-run", "BTC-USDT"
    ) == Decimal("0.0004")


def account_payload(timestamp: int, total: str) -> dict[str, object]:
    return {
        "uTime": str(timestamp),
        "details": [
            {
                "ccy": "USDT",
                "cashBal": total,
                "availBal": total,
                "frozenBal": "0",
                "eq": total,
                "eqUsd": total,
                "uTime": str(timestamp),
            }
        ],
    }


def test_account_push_updates_transient_snapshot_and_duplicate_is_ignored(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'account-event.db'}")
    database.initialize()
    processor = PrivateEventProcessor(TradingRepository(database))
    event = PrivateEvent(
        PrivateEventKind.ACCOUNT,
        "account:new",
        account_payload(2_000, "100"),
    )
    assert processor.process(event)
    assert not processor.process(event)
    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            """SELECT event_time, needs_reconciliation
            FROM private_state_snapshots WHERE scope_key = 'account:USDT'"""
        ).fetchone()
    assert row == (datetime.fromtimestamp(2, tz=UTC).isoformat(), 1)


def test_older_account_push_does_not_overwrite_newer_state(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'ordered-event.db'}")
    database.initialize()
    processor = PrivateEventProcessor(TradingRepository(database))
    assert processor.process(
        PrivateEvent(
            PrivateEventKind.ACCOUNT,
            "account:new",
            account_payload(2_000, "100"),
        )
    )
    assert processor.process(
        PrivateEvent(
            PrivateEventKind.ACCOUNT,
            "account:old",
            account_payload(1_000, "50"),
        )
    )
    with sqlite3.connect(database.path) as connection:
        payload = connection.execute(
            """SELECT normalized_json FROM private_state_snapshots
            WHERE scope_key = 'account:USDT'"""
        ).fetchone()[0]
    assert json.loads(payload)["balances"]["USDT"]["cash_balance"] == "100"


def test_balance_and_position_push_updates_transient_snapshot(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'position-event.db'}")
    database.initialize()
    processor = PrivateEventProcessor(TradingRepository(database))
    event = PrivateEvent(
        PrivateEventKind.POSITION,
        "position:1",
        {
            "pTime": "2000",
            "balData": [{"ccy": "BTC", "cashBal": "0.5", "uTime": "2000"}],
            "posData": [],
        },
    )
    assert processor.process(event)
    with sqlite3.connect(database.path) as connection:
        payload = connection.execute(
            """SELECT normalized_json FROM private_state_snapshots
            WHERE scope_key = 'position:BTC'"""
        ).fetchone()[0]
    assert json.loads(payload)["balances"]["BTC"]["cash_balance"] == "0.5"
