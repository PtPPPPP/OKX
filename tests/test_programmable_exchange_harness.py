from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.market import Instrument
from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType
from app.domain.position import PortfolioSnapshot
from app.exchange.exceptions import ExchangeError
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.runtime.clock import BacktestClock
from app.services.private_events import PrivateEventProcessor
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.reconciliation import AccountSync, ReconciliationService, ReconciliationStatus
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from tests.conftest import make_candles, make_instrument
from tests.programmable_exchange import ProgrammableExchange


def _account_event(key: str, amount: str, sequence: int) -> PrivateEvent:
    return PrivateEvent(
        PrivateEventKind.ACCOUNT,
        key,
        {
            "uTime": str(1_000 + sequence),
            "details": [
                {
                    "ccy": "USDT",
                    "cashBal": amount,
                    "availBal": amount,
                    "frozenBal": "0",
                    "eq": amount,
                    "uTime": str(1_000 + sequence),
                }
            ],
        },
        connection_epoch=1,
        sequence=sequence,
    )


def _order(instrument: Instrument, state: OrderState) -> Order:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    request = OrderRequest(
        "programmable-order",
        instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100"),
        "programmable-exchange",
        now,
        run_id="programmable-run",
        strategy_name="moving_average_cross",
        mode="demo",
        bar="5m",
    )
    return Order(request, state=state, updated_at=now)


def _ready_harness(
    tmp_path: Path,
) -> tuple[TradingRepository, ProgrammableExchange, PrivateStateCoordinator, Instrument]:
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={instrument.instrument_id: Decimal("0")},
        average_entry_prices={},
    )
    candles = make_candles(["100", "101"])
    exchange = ProgrammableExchange(portfolio, candles)
    database = Database(f"sqlite:///{tmp_path / 'programmable-exchange.db'}")
    database.initialize()
    repository = TradingRepository(database)
    AccountSync(exchange, repository, BacktestClock(candles[-1].timestamp)).sync(
        instrument,
        "5m",
        run_id="programmable-run",
        mode="demo",
        strategy_name="moving_average_cross",
    )
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository), ReconciliationService(exchange, repository), repository
    )
    return repository, exchange, coordinator, instrument


def test_programmable_exchange_replays_private_events_after_delayed_rest(
    tmp_path: Path,
) -> None:
    repository, exchange, coordinator, instrument = _ready_harness(tmp_path)
    gate = exchange.block_next_pending_orders()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(coordinator.reconcile_private_state, instrument, source="test")
        assert gate.entered.wait(timeout=2)
        assert coordinator.handle_private_ws_event(_account_event("account:one", "110", 1))
        assert coordinator.handle_private_ws_event(_account_event("account:two", "120", 2))
        gate.release.set()
        result = future.result(timeout=2)

    assert result.status is ReconciliationStatus.HEALTHY
    assert repository.private_state_snapshot().ws_watermark == 2
    with repository.database.connect() as connection:
        payload = connection.execute(
            "SELECT normalized_json FROM private_state_snapshots WHERE scope_key='account:USDT'"
        ).fetchone()[0]
    assert '"cash_balance": "120"' in payload
    assert exchange.broker_write_calls == 0


def test_programmable_exchange_stale_rest_order_cannot_regress_private_fill(
    tmp_path: Path,
) -> None:
    repository, exchange, coordinator, instrument = _ready_harness(tmp_path)
    stale_order = _order(instrument, OrderState.ACCEPTED)
    repository.save_order(stale_order)
    exchange.pending_orders = [stale_order]
    gate = exchange.block_next_pending_orders()
    filled_order = _order(instrument, OrderState.FILLED)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(coordinator.reconcile_private_state, instrument, source="test")
        assert gate.entered.wait(timeout=2)
        assert coordinator.handle_private_ws_event(
            PrivateEvent(
                PrivateEventKind.ORDER,
                "order:filled",
                {"uTime": "1001"},
                order=filled_order,
                connection_epoch=1,
                sequence=1,
            )
        )
        gate.release.set()
        result = future.result(timeout=2)

    assert result.status is ReconciliationStatus.HEALTHY
    loaded = repository.load_order("programmable-order")
    assert loaded is not None
    assert loaded.state is OrderState.FILLED
    assert exchange.broker_write_calls == 0


def test_programmable_exchange_rest_failure_and_unknown_order_freeze_state(
    tmp_path: Path,
) -> None:
    repository, exchange, coordinator, instrument = _ready_harness(tmp_path)
    exchange.fail_next_pending_orders(ExchangeError("programmed REST failure"))

    failed = coordinator.reconcile_private_state(instrument, source="test")

    assert failed.status is ReconciliationStatus.UNKNOWN
    assert not repository.private_state_snapshot().submission_allowed

    repository.save_order(_order(instrument, OrderState.UNKNOWN))
    blocked = coordinator.reconcile_private_state(instrument, source="test")

    assert blocked.status is ReconciliationStatus.BLOCKED
    assert not repository.private_state_snapshot().submission_allowed
    assert exchange.broker_write_calls == 0


def test_programmable_exchange_private_sequence_gap_freezes_before_rest(
    tmp_path: Path,
) -> None:
    repository, exchange, coordinator, _ = _ready_harness(tmp_path)

    assert not coordinator.handle_private_ws_event(_account_event("account:gap", "100", 2))

    snapshot = repository.private_state_snapshot()
    assert not snapshot.submission_allowed
    assert snapshot.ws_watermark == 0
    assert exchange.broker_write_calls == 0


def test_programmable_exchange_snapshot_delta_mismatch_freezes_then_recovers(
    tmp_path: Path,
) -> None:
    repository, exchange, coordinator, instrument = _ready_harness(tmp_path)

    exchange.portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("99")},
        positions={instrument.instrument_id: Decimal("0")},
        average_entry_prices={},
    )
    gate = exchange.block_next_pending_orders()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            coordinator.reconcile_private_state,
            instrument,
            source="snapshot-delta-mismatch",
        )
        assert gate.entered.wait(timeout=2)
        assert coordinator.handle_private_ws_event(_account_event("delta:mismatch", "120", 1))
        gate.release.set()
        blocked = future.result(timeout=2)

    assert blocked.status is ReconciliationStatus.BLOCKED
    assert not repository.private_state_snapshot().submission_allowed

    exchange.portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={instrument.instrument_id: Decimal("0")},
        average_entry_prices={},
    )
    recovered = coordinator.reconcile_private_state(instrument, source="snapshot-delta-recovery")

    assert recovered.status is ReconciliationStatus.HEALTHY
    assert repository.private_state_snapshot().submission_allowed
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0
