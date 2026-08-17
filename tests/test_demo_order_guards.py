from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import RunConfig, load_run_config
from app.domain.market import Candle, Instrument
from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType
from app.domain.position import PortfolioSnapshot
from app.exchange.exceptions import ExchangeError, OrderNotFound
from app.execution.demo_write_authorization import DemoWriteAuthorization
from app.portfolio.cost_basis import CostFill
from app.runtime.clock import SystemClock
from app.services.demo_orders import DemoOrderService
from app.services.reconciliation import AccountSnapshot, ReconciliationResult, ReconciliationStatus
from app.storage.database import Database, StorageError
from app.storage.repositories import TradingRepository
from tests.conftest import make_candles


class Gate:
    def __init__(
        self,
        ready: bool,
        reconciliation: ReconciliationResult | None = None,
        account_error: Exception | None = None,
    ) -> None:
        self.order_submission_ready = ready
        self.reconciliation = reconciliation
        self.account_error = account_error

    def reconcile_result(self) -> ReconciliationResult:
        return self.reconciliation or ReconciliationResult(ReconciliationStatus.HEALTHY, "ok", 0, 0)

    def synchronize_private_account(self, *, run_id: str) -> AccountSnapshot:
        if self.account_error is not None:
            raise self.account_error
        portfolio = PortfolioSnapshot(
            balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
            positions={"BTC-USDT": Decimal("0")},
            average_entry_prices={},
        )
        return AccountSnapshot(portfolio, Decimal("100"), datetime.now(UTC), ())


class FakeDemoClient:
    def __init__(self, instrument: Instrument, *, fail_sync: bool = False) -> None:
        self.instrument = instrument
        self.fail_sync = fail_sync
        self.place_called = False
        self.clock = SystemClock()
        self.candles = make_candles(["100", "101"])

    def get_instrument(self, instrument_id: str) -> Instrument:
        return self.instrument

    def get_portfolio(self, instrument: Instrument) -> PortfolioSnapshot:
        if self.fail_sync:
            raise ExchangeError("injected sync failure")
        return PortfolioSnapshot(
            balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
            positions={instrument.instrument_id: Decimal("0")},
            average_entry_prices={},
        )

    def get_pending_orders(self, instrument_id: str) -> list[Order]:
        return []

    def get_history_candles(
        self, instrument_id: str, bar: str = "5m", limit: int = 300
    ) -> list[Candle]:
        return self.candles[-limit:]

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]:
        return []

    def get_derivative_positions(self) -> dict[str, Decimal]:
        return {}

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        raise OrderNotFound("not found")

    def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        raise OrderNotFound("not found")

    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        self.place_called = True
        raise AssertionError("guard failure must happen before remote order")


class FailingRepository(TradingRepository):
    def save_portfolio_snapshot(self, *args: object, **kwargs: object) -> None:
        raise StorageError("injected database failure")


def config() -> RunConfig:
    return load_run_config(Path("configs/btc_ma_demo.yaml"), environ={})


def repository(tmp_path: Path) -> TradingRepository:
    database = Database(f"sqlite:///{tmp_path / 'demo-guards.db'}")
    database.initialize()
    return TradingRepository(database)


def test_private_websocket_not_ready_rejects_before_sync(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    client = FakeDemoClient(btc_instrument)
    service = DemoOrderService(config(), client, repository(tmp_path), submission_gate=Gate(False))
    with pytest.raises(PermissionError, match="persisted Proposal"):
        service.submit(side=OrderSide.BUY, price=Decimal("100"))
    assert not client.place_called


def test_account_sync_failure_rejects_order(tmp_path: Path, btc_instrument: Instrument) -> None:
    client = FakeDemoClient(btc_instrument, fail_sync=True)
    service = DemoOrderService(
        config(),
        client,
        repository(tmp_path),
        submission_gate=Gate(True, account_error=ExchangeError("injected sync failure")),
    )
    with pytest.raises(PermissionError, match="persisted Proposal"):
        service.submit(side=OrderSide.BUY, price=Decimal("100"))
    assert not client.place_called


def test_database_failure_rejects_before_remote_order(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'database-failure.db'}")
    database.initialize()
    client = FakeDemoClient(btc_instrument)
    service = DemoOrderService(
        config(),
        client,
        FailingRepository(database),
        submission_gate=Gate(True, account_error=StorageError("injected database failure")),
    )
    with pytest.raises(PermissionError, match="persisted Proposal"):
        service.submit(side=OrderSide.BUY, price=Decimal("100"))
    assert not client.place_called


def test_unknown_local_order_blocks_new_submission(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    repo = repository(tmp_path)
    now = datetime.now(UTC)
    request = OrderRequest(
        "unknown",
        btc_instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100"),
        "signal",
        now,
        run_id="run",
        strategy_name="moving_average_cross",
        mode="demo",
        bar="5m",
    )
    repo.save_order(Order(request, state=OrderState.UNKNOWN, updated_at=now))
    client = FakeDemoClient(btc_instrument)
    service = DemoOrderService(
        config(),
        client,
        repo,
        submission_gate=Gate(
            True,
            ReconciliationResult(ReconciliationStatus.BLOCKED, "存在未知委托", 0, 1),
        ),
    )
    with pytest.raises(PermissionError, match="persisted Proposal"):
        service.submit(side=OrderSide.BUY, price=Decimal("100"))
    assert not client.place_called
