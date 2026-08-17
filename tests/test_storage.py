from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.events import DomainEvent, EventBus
from app.domain.market import Instrument
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
    AccountMode,
    AssetBalance,
    BalanceSource,
    BalanceValidationStatus,
    Portfolio,
    PortfolioSnapshot,
)
from app.storage.database import Database
from app.storage.repositories import TradingRepository


def test_order_runtime_and_generic_snapshots_survive_restart(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database_url = f"sqlite:///{tmp_path / 'trading.db'}"
    database = Database(database_url)
    database.initialize()
    repository = TradingRepository(database)
    request = OrderRequest(
        "client-1",
        btc_instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100.0"),
        "signal-1",
        datetime.now(UTC),
        run_id="run",
        strategy_name="buy_and_hold",
        mode="demo",
        bar="5m",
        order_source=OrderSource.MANUAL_DEMO_TEST,
    )
    order = Order(request)
    order.transition(OrderState.SUBMITTED, at=datetime.now(UTC))
    order.transition(OrderState.UNKNOWN, at=datetime.now(UTC))
    repository.save_order(order)
    repository.save_runtime_mode("demo")
    portfolio = Portfolio({"USDT": Decimal("100")}, {btc_instrument.instrument_id: Decimal("0.1")})
    repository.save_portfolio_snapshot(
        portfolio.snapshot(),
        btc_instrument,
        Decimal("100"),
        datetime.now(UTC),
        run_id="run",
        mode="demo",
        strategy_name="buy_and_hold",
        bar="5m",
    )
    repository.save_audit_record(
        record_type="strategy_config",
        run_id="run",
        mode="backtest",
        strategy_name="buy_and_hold",
        instrument_id=btc_instrument.instrument_id,
        bar="5m",
        payload={"parameters": {}},
    )

    restarted = TradingRepository(Database(database_url))
    restored = restarted.load_order("client-1")
    assert restored is not None
    assert restored.state is OrderState.UNKNOWN
    assert restored.request.run_id == "run"
    assert restored.request.strategy_name == "buy_and_hold"
    assert restored.request.mode == "demo"
    assert restored.request.bar == "5m"
    assert restored.request.order_source is OrderSource.MANUAL_DEMO_TEST
    assert restarted.load_open_orders()[0].request.client_order_id == "client-1"
    assert restarted.load_runtime_mode() == "demo"
    restored_portfolio = restarted.load_latest_portfolio_snapshot(btc_instrument)
    assert restored_portfolio is not None
    assert restored_portfolio[0].position(btc_instrument.instrument_id) == Decimal("0.1")
    with database.connect() as connection:
        state = connection.execute(
            """SELECT run_id, mode, strategy_name, instrument_id, bar, signal_id,
                      order_source
            FROM order_state_changes WHERE client_order_id = 'client-1'"""
        ).fetchone()
    assert tuple(state) == (
        "run",
        "demo",
        "buy_and_hold",
        "BTC-USDT",
        "5m",
        "signal-1",
        "manual_demo_test",
    )


def test_fill_and_event_idempotency_survive_restart(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'idempotency.db'}")
    database.initialize()
    repository = TradingRepository(database)
    now = datetime.now(UTC)

    assert repository.save_fill(
        "client-1",
        OrderSide.BUY,
        Decimal("0.1"),
        Decimal("100"),
        Decimal("0.01"),
        now,
        fill_id="fill-1",
        exchange_fill_id="trade-1",
    )
    assert not repository.save_fill(
        "client-1",
        OrderSide.BUY,
        Decimal("0.1"),
        Decimal("100"),
        Decimal("0.01"),
        now,
        fill_id="fill-1",
        exchange_fill_id="trade-1",
    )
    event = DomainEvent(
        run_id="run",
        timestamp=now,
        event_type="FillReceived",
        instrument_id="BTC-USDT",
        strategy_name="buy_and_hold",
        payload={"fill_id": "fill-1"},
        idempotency_key="fill:trade-1",
    )
    assert EventBus(repository).publish(event)
    assert not EventBus(repository).publish(event)
    conflicting = DomainEvent(
        run_id="run",
        timestamp=now,
        event_type="FillReceived",
        instrument_id="BTC-USDT",
        strategy_name="buy_and_hold",
        payload={"fill_id": "different"},
        idempotency_key="fill:trade-1",
    )
    with pytest.raises(ValueError, match="不同事件或负载"):
        EventBus(repository).publish(conflicting)
    with database.connect() as connection:
        fill = connection.execute(
            """SELECT data_quality_status, quarantine_reason, eligible_for_cost_basis
            FROM fills"""
        ).fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]
    assert tuple(fill) == ("quarantined", "missing_parent_order", 0)
    assert event_count == 1


def test_account_snapshot_round_trip_keeps_raw_fields_separate(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'account-snapshot.db'}")
    database.initialize()
    repository = TradingRepository(database)
    now = datetime.now(UTC)
    asset = AssetBalance(
        "USDT",
        Decimal("10"),
        Decimal("7"),
        Decimal("3"),
        Decimal("10"),
        Decimal("10"),
        None,
        None,
        None,
        Decimal("10"),
        Decimal("7"),
        AccountMode.SPOT,
        BalanceSource.REST,
        now,
        frozenset({"cashBal", "availBal", "frozenBal"}),
        True,
        BalanceValidationStatus.PASSED,
    )
    snapshot = PortfolioSnapshot(
        {"USDT": Decimal("10")},
        {btc_instrument.instrument_id: Decimal("0")},
        {},
        asset_balances={"USDT": asset},
        account_configuration=AccountConfiguration(AccountMode.SPOT, None, None, None, now),
    )
    repository.save_portfolio_snapshot(
        snapshot,
        btc_instrument,
        Decimal("100"),
        now,
        run_id="run",
        mode="demo",
        strategy_name="test",
        bar="5m",
    )
    restored = repository.load_latest_portfolio_snapshot(btc_instrument)
    assert restored is not None
    assert restored[0].asset_balances["USDT"].cash_balance == Decimal("10")
    assert restored[0].asset_balances["USDT"].available_balance == Decimal("7")
    assert restored[0].asset_balances["USDT"].frozen_balance == Decimal("3")


def test_repository_rejects_order_state_regression(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orders.db'}")
    database.initialize()
    repository = TradingRepository(database)
    now = datetime.now(UTC)
    request = OrderRequest(
        "client-1",
        btc_instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100"),
        "signal",
        now,
    )
    accepted = Order(request, state=OrderState.ACCEPTED, updated_at=now)
    repository.save_order(accepted)
    with pytest.raises(ValueError, match="状态回退"):
        repository.save_order(Order(request, state=OrderState.SUBMITTED, updated_at=now))
