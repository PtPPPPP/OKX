from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.market import Candle, Instrument
from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType
from app.domain.position import PortfolioSnapshot
from app.exchange.exceptions import OrderNotFound
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.portfolio.cost_basis import CostFill
from app.runtime.clock import BacktestClock
from app.services.private_events import PrivateEventProcessor
from app.services.reconciliation import (
    AccountSync,
    ReconciliationService,
    ReconciliationStatus,
)
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from tests.conftest import make_candles


class FakeClient:
    def __init__(
        self,
        portfolio: PortfolioSnapshot,
        candles: list[Candle],
        orders: list[Order] | None = None,
        derivative_positions: dict[str, Decimal] | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.candles = candles
        self.orders = orders or []
        self.queries: dict[str, Order] = {}
        self.derivative_positions = derivative_positions or {}

    def get_portfolio(self, instrument: Instrument) -> PortfolioSnapshot:
        return self.portfolio

    def get_pending_orders(self, instrument_id: str) -> list[Order]:
        return self.orders

    def get_history_candles(
        self, instrument_id: str, bar: str = "5m", limit: int = 300
    ) -> list[Candle]:
        return self.candles[-limit:]

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        try:
            return self.queries[client_order_id]
        except KeyError as exc:
            raise OrderNotFound("fixture not found") from exc

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]:
        return []

    def get_derivative_positions(self) -> dict[str, Decimal]:
        return self.derivative_positions


def _unknown_order(instrument: Instrument) -> Order:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = OrderRequest(
        "unknown-1",
        instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.001"),
        Decimal("100"),
        "signal",
        now,
    )
    return Order(request, state=OrderState.UNKNOWN, updated_at=now)


def test_account_sync_then_reconciliation_is_healthy(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'sync.db'}")
    database.initialize()
    repository = TradingRepository(database)
    portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={"BTC-USDT": Decimal("0")},
        average_entry_prices={},
    )
    candles = make_candles(["100", "101"])
    client = FakeClient(portfolio, candles)
    AccountSync(client, repository, BacktestClock(candles[-1].timestamp)).sync(
        btc_instrument,
        "5m",
        run_id="run",
        mode="demo",
        strategy_name="buy_and_hold",
    )
    result = ReconciliationService(client, repository).reconcile(btc_instrument)
    assert result.status is ReconciliationStatus.HEALTHY
    assert result.order_submission_allowed


def test_unresolved_unknown_order_blocks_submission(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'blocked.db'}")
    database.initialize()
    repository = TradingRepository(database)
    portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={"BTC-USDT": Decimal("0")},
        average_entry_prices={},
    )
    candles = make_candles(["100"])
    client = FakeClient(portfolio, candles)
    AccountSync(client, repository, BacktestClock(candles[0].timestamp)).sync(
        btc_instrument,
        "5m",
        run_id="run",
        mode="demo",
        strategy_name="buy_and_hold",
    )
    repository.save_order(_unknown_order(btc_instrument))

    result = ReconciliationService(client, repository).reconcile(btc_instrument)
    assert result.status is ReconciliationStatus.BLOCKED
    assert result.unresolved_order_ids == ("unknown-1",)
    assert repository.load_order("unknown-1").state is OrderState.UNKNOWN  # type: ignore[union-attr]


def test_portfolio_mismatch_blocks_submission(tmp_path: Path, btc_instrument: Instrument) -> None:
    database = Database(f"sqlite:///{tmp_path / 'mismatch.db'}")
    database.initialize()
    repository = TradingRepository(database)
    local = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={"BTC-USDT": Decimal("0")},
        average_entry_prices={},
    )
    remote = PortfolioSnapshot(
        balances={"BTC": Decimal("0.1"), "USDT": Decimal("80")},
        positions={"BTC-USDT": Decimal("0.1")},
        average_entry_prices={},
    )
    candles = make_candles(["100"])
    AccountSync(FakeClient(local, candles), repository, BacktestClock(candles[0].timestamp)).sync(
        btc_instrument,
        "5m",
        run_id="run",
        mode="demo",
        strategy_name="buy_and_hold",
    )
    result = ReconciliationService(FakeClient(remote, candles), repository).reconcile(
        btc_instrument
    )
    assert result.status is ReconciliationStatus.BLOCKED
    assert "不一致" in result.message


def test_direct_rest_reconciliation_does_not_confirm_private_websocket_transient_state(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'ws-confirm.db'}")
    database.initialize()
    repository = TradingRepository(database)
    portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={"BTC-USDT": Decimal("0")},
        average_entry_prices={},
    )
    candles = make_candles(["100"])
    client = FakeClient(portfolio, candles)
    AccountSync(client, repository, BacktestClock(candles[0].timestamp)).sync(
        btc_instrument,
        "5m",
        run_id="run",
        mode="demo",
        strategy_name="buy_and_hold",
    )
    event = PrivateEvent(
        PrivateEventKind.POSITION,
        "position:confirm",
        {
            "pTime": "1000",
            "balData": [{"ccy": "BTC", "cashBal": "0", "uTime": "1000"}],
            "posData": [],
        },
    )
    assert PrivateEventProcessor(repository).process(event)
    assert repository.has_unreconciled_private_state()
    result = ReconciliationService(client, repository).reconcile(btc_instrument)
    assert result.status is ReconciliationStatus.HEALTHY
    assert repository.has_unreconciled_private_state()


def test_rest_derivative_position_blocks_spot_submission(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'derivative-position.db'}")
    database.initialize()
    repository = TradingRepository(database)
    portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={"BTC-USDT": Decimal("0")},
        average_entry_prices={},
    )
    candles = make_candles(["100"])
    client = FakeClient(
        portfolio,
        candles,
        derivative_positions={"SPY-USDT-SWAP": Decimal("1")},
    )
    AccountSync(client, repository, BacktestClock(candles[0].timestamp)).sync(
        btc_instrument,
        "5m",
        run_id="run",
        mode="demo",
        strategy_name="buy_and_hold",
    )
    result = ReconciliationService(client, repository).reconcile(btc_instrument)
    assert result.status is ReconciliationStatus.BLOCKED
    assert "REST" in result.message
