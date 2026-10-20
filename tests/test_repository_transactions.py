from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.run_config import load_run_config
from app.domain.account import PrivateAccountState
from app.domain.capability import MaxAvailableSize
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import Order, OrderSide, OrderState, OrderType
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    DemoOrderProposal,
    ProposalStatus,
)
from app.storage.database import Database, StorageError
from app.storage.repositories import TradingRepository
from tests.conftest import make_instrument


def make_prepared_repository(tmp_path: Path) -> tuple[TradingRepository, DemoOrderProposal]:
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'transactions.db'}"))
    repository.database.initialize()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.001", "0.1")
    proposal = DemoOrderPreflightService().prepare_order(
        intent=DemoOrderIntent(
            "run",
            "moving_average_cross",
            "BTC-USDT",
            InstrumentType.SPOT,
            TradeMode.CASH,
            OrderSide.BUY,
            OrderType.LIMIT,
            Decimal("5"),
            "manual_demo_test",
            now,
        ),
        config=load_run_config(Path("configs/btc_ma_demo.yaml"), environ={}),
        instrument=instrument,
        portfolio=PortfolioSnapshot(
            {"USDT": Decimal("100")},
            {"BTC-USDT": Decimal("0")},
            {},
            account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, now),
        ),
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("100"), Decimal("1"), now),
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("100"),
        now=now,
    )
    assert proposal.status is ProposalStatus.READY_FOR_CONFIRMATION
    repository.save_demo_order_proposal(proposal)
    repository.confirm_private_state_snapshots(now)
    return repository, proposal


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[TradingRepository, DemoOrderProposal]:
    return make_prepared_repository(tmp_path)


def _dump(database: Database) -> list[str]:
    with database.connect() as connection:
        return list(connection.iterdump())


class _FailAfterWrite:
    """Fail after SQLite applied a statement, before its transaction can commit."""

    def __init__(self, connection: sqlite3.Connection, stage: int, error: type[Exception]) -> None:
        self.connection = connection
        self.stage = stage
        self.error = error
        self.writes = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        result = self.connection.execute(sql, *args)
        if sql.lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE"}:
            self.writes += 1
            if self.writes == self.stage:
                raise self.error("injected after write")
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


# Every modifying stage of the existing cross-table transactions, including
# the final statement. Both SQLite and business exceptions must leave no writes.
_STAGES = {
    "fence": 2,
    "begin": 4,
    "authorize": 1,
    "complete": 3,
    "unknown": 4,
    "recover": 4,
    "private_event": 4,
    "save_order": 2,
    "managed_fill": 2,
}


@pytest.mark.parametrize("error", [RuntimeError, sqlite3.OperationalError])
@pytest.mark.parametrize(
    ("operation", "stage"),
    [(operation, stage) for operation, count in _STAGES.items() for stage in range(1, count + 1)],
)
def test_atomic_transactions_rollback_every_stage(
    prepared: tuple[TradingRepository, DemoOrderProposal],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    stage: int,
    error: type[Exception],
) -> None:
    repository, proposal = prepared
    database = repository.database
    local: Order | None = None
    if operation not in {"fence", "private_event", "managed_fill"}:
        repository.fence_demo_order_proposal(proposal.proposal_id)
        if operation != "begin":
            local = repository.begin_controlled_demo_submission(proposal)
    if operation == "recover":
        repository.mark_controlled_demo_submission_unknown(
            proposal.proposal_id, error_category="test", http_status=None
        )
    before = _dump(database)
    original_open = database._open_connection
    opened: list[_FailAfterWrite] = []

    def failing_open(durability: Any) -> Any:
        connection = _FailAfterWrite(original_open(durability), stage, error)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(database, "_open_connection", failing_open)
        with pytest.raises((RuntimeError, StorageError), match="injected after write"):
            if operation == "fence":
                repository.fence_demo_order_proposal(proposal.proposal_id)
            elif operation == "begin":
                repository.begin_controlled_demo_submission(proposal)
            elif operation == "authorize":
                repository.authorize_controlled_demo_write(
                    proposal.proposal_id, proposal.client_order_id, operation="place"
                )
            elif operation == "complete":
                assert local is not None
                repository.complete_controlled_demo_submission(
                    proposal.proposal_id,
                    replace(local, state=OrderState.ACCEPTED),
                    event_type="submission_succeeded",
                    proposal_status=ProposalStatus.SUBMITTED,
                )
            elif operation == "unknown":
                repository.mark_controlled_demo_submission_unknown(
                    proposal.proposal_id, error_category="test", http_status=None
                )
            elif operation == "recover":
                assert local is not None
                repository.resolve_unknown_order_from_authoritative_read(
                    proposal.proposal_id,
                    replace(local, state=OrderState.ACCEPTED, exchange_order_id="remote"),
                )
            elif operation == "private_event":
                repository.apply_private_state_event(
                    "event",
                    "account",
                    "hash",
                    PrivateAccountState("account", datetime.now(UTC), {}, {}),
                )
            elif operation == "save_order":
                assert local is not None
                repository.save_order(replace(local, state=OrderState.ACCEPTED))
            else:
                repository.apply_managed_fill(
                    strategy_name="test",
                    run_id="run",
                    instrument_id="BTC-USDT",
                    side=OrderSide.BUY,
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                )
    assert len(opened) == 1  # No delegated store silently opened another transaction.
    assert opened[0].writes == stage
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].connection.execute("SELECT 1")
    assert _dump(database) == before
    assert database.safe_stopped is (error is sqlite3.OperationalError)


def test_facade_and_stores_share_persisted_state_and_idempotency(
    prepared: tuple[TradingRepository, DemoOrderProposal],
) -> None:
    repository, proposal = prepared
    for name in ("orders", "accounts", "audit", "proposals", "private_state"):
        assert getattr(repository, name).database is repository.database
    repository.proposals.fence_demo_order_proposal(proposal.proposal_id)
    local = repository.begin_controlled_demo_submission(proposal)
    assert repository.orders.load_order(local.request.client_order_id) == repository.load_order(
        local.request.client_order_id
    )
    with pytest.raises(ValueError):
        repository.proposals.begin_controlled_demo_submission(proposal)
    repository.proposals.authorize_controlled_demo_write(
        proposal.proposal_id, proposal.client_order_id, operation="place"
    )
    with pytest.raises(PermissionError, match="already issued"):
        repository.authorize_controlled_demo_write(
            proposal.proposal_id, proposal.client_order_id, operation="place"
        )
    now = datetime.now(UTC)
    assert repository.orders.save_fill(
        proposal.client_order_id,
        OrderSide.BUY,
        Decimal("1"),
        Decimal("100"),
        Decimal("0"),
        now,
        fill_id="unique",
    )
    assert not repository.save_fill(
        proposal.client_order_id,
        OrderSide.BUY,
        Decimal("1"),
        Decimal("100"),
        Decimal("0"),
        now,
        fill_id="unique",
    )
    state = PrivateAccountState("account", now, {}, {})
    assert repository.private_state.apply_private_state_event("private", "account", "hash", state)
    assert not repository.apply_private_state_event("private", "account", "hash", state)
    assert repository.audit.claim_event("audit", "test", "hash")
    assert not repository.claim_event("audit", "test", "hash")
    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM bounded_submission_events").fetchone()[0] == 1
        )
