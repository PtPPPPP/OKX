from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from app.domain.context import MarketSnapshot
from app.domain.market import Instrument
from app.domain.order import (
    ApprovedOrder,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    ProposedOrder,
)
from app.domain.position import Portfolio
from app.domain.signal import Signal, SignalAction
from app.exchange.okx_client import OkxClient
from app.execution.backtest_broker import BacktestBroker
from app.execution.demo_broker import OKXDemoBroker
from app.market.providers import CSVMarketDataProvider
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.session import TradingSession
from app.storage.database import StorageError
from app.storage.repositories import TradingRepository
from app.strategies.moving_average import MovingAverageCrossStrategy
from app.trading_engine import TradingEngine
from tests.conftest import make_candles


def proposed(instrument: Instrument) -> ProposedOrder:
    return ProposedOrder(
        client_order_id="client",
        run_id="run",
        strategy_name="buy_and_hold",
        instrument_id=instrument.instrument_id,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=instrument.quantity_step,
        price=Decimal("100.0"),
        signal_id="signal",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_position_sizer_uses_each_instruments_quantity_step(
    btc_instrument: Instrument, eth_instrument: Instrument
) -> None:
    sizer = FixedNotionalPositionSizer(Decimal("20"))
    candle = make_candles(["3000"])[0]
    signal = Signal(
        "signal",
        "buy_and_hold",
        btc_instrument.instrument_id,
        SignalAction.BUY,
        candle.timestamp,
        "test",
        Decimal("1"),
    )
    portfolio = Portfolio({"USDT": Decimal("100")}).snapshot()
    btc = sizer.calculate(
        signal, portfolio, MarketSnapshot(candle, Decimal("3000")), btc_instrument
    )
    eth = sizer.calculate(
        signal, portfolio, MarketSnapshot(candle, Decimal("3000")), eth_instrument
    )
    assert btc.quantity % btc_instrument.quantity_step == 0
    assert eth.quantity % eth_instrument.quantity_step == 0
    assert btc.quantity != eth.quantity


def test_csv_provider_is_replaceable(tmp_path: Path) -> None:
    from app.market.historical_data import save_candles_csv

    path = tmp_path / "bars.csv"
    candles = make_candles(["100", "101"])
    save_candles_csv(candles, path)
    loaded = CSVMarketDataProvider(path).get_historical_bars("ANY-SPOT", "5m")
    assert loaded == candles


class FakeClient:
    def place_order(self, request: OrderRequest) -> Order:
        order = Order(request)
        order.transition(OrderState.SUBMITTED, at=request.created_at)
        order.transition(OrderState.FILLED, at=request.created_at)
        return order


class FakeRepository:
    def save_order(self, order: Order) -> None:
        return None


class FailingPersistenceRepository:
    def __init__(self) -> None:
        self.calls = 0

    def save_order(self, order: Order) -> None:
        self.calls += 1
        if self.calls == 2:
            raise StorageError("injected persistence failure")


class CountingClient(FakeClient):
    def __init__(self) -> None:
        self.place_count = 0

    def place_order(self, request: OrderRequest) -> Order:
        self.place_count += 1
        return super().place_order(request)


def test_brokers_accept_same_approved_order_model(btc_instrument: Instrument) -> None:
    approved = ApprovedOrder(proposed(btc_instrument), datetime(2026, 1, 1, tzinfo=UTC), "approved")
    portfolio = Portfolio({"USDT": Decimal("100")}, {btc_instrument.instrument_id: Decimal("0")})
    backtest = BacktestBroker(portfolio, btc_instrument, Decimal("0.001"), Decimal("0.0005"))
    demo = OKXDemoBroker(cast(OkxClient, FakeClient()), cast(TradingRepository, FakeRepository()))
    assert backtest.submit_order(approved).state is OrderState.FILLED
    with pytest.raises(PermissionError, match="Proposal gate"):
        demo.submit_order(approved)


def test_demo_broker_direct_submit_fails_before_client_or_persistence(
    btc_instrument: Instrument,
) -> None:
    approved = ApprovedOrder(proposed(btc_instrument), datetime(2026, 1, 1, tzinfo=UTC), "approved")
    client = CountingClient()
    repository = FailingPersistenceRepository()
    broker = OKXDemoBroker(cast(OkxClient, client), cast(TradingRepository, repository))
    with pytest.raises(PermissionError, match="Proposal gate"):
        broker.submit_order(approved)
    assert client.place_count == 0
    assert repository.calls == 0


def test_demo_broker_direct_place_and_cancel_require_authorization(
    btc_instrument: Instrument,
) -> None:
    client = CountingClient()
    repository = FailingPersistenceRepository()
    broker = OKXDemoBroker(cast(OkxClient, client), cast(TradingRepository, repository))

    with pytest.raises(PermissionError, match="one-use authorization"):
        broker.place_order(proposed(btc_instrument).to_request())
    with pytest.raises(PermissionError, match="one-use authorization"):
        broker.cancel_order("BTC-USDT", "client")

    assert client.place_count == 0
    assert repository.calls == 0


def test_strategy_source_has_no_exchange_or_broker_dependency() -> None:
    source = inspect.getsource(MovingAverageCrossStrategy)
    assert "OkxClient" not in source
    assert "Broker" not in source
    assert ".env" not in source


def test_core_engine_and_session_do_not_depend_on_okx_or_cli() -> None:
    engine_source = inspect.getsource(TradingEngine)
    session_source = inspect.getsource(TradingSession)
    assert "OkxClient" not in engine_source
    assert "OKX" not in engine_source
    assert "app.cli" not in engine_source
    assert "BacktestEngine" not in session_source
    assert "OkxClient" not in session_source


def test_business_modules_cannot_call_low_level_demo_writes() -> None:
    app_root = Path(__file__).parents[1] / "app"
    allowed = {
        app_root / "execution" / "demo_broker.py",
        app_root / "services" / "controlled_demo_write.py",
    }
    violations: list[str] = []
    for path in app_root.rglob("*.py"):
        if path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            direct_client = isinstance(receiver, ast.Name) and receiver.id == "client"
            client_attribute = isinstance(receiver, ast.Attribute) and receiver.attr == "client"
            if node.func.attr in {"place_order", "cancel_order", "_request"} and (
                direct_client or client_attribute
            ):
                violations.append(f"{path.relative_to(app_root)}:{node.lineno}")
    assert violations == []


def test_okx_demo_broker_has_no_production_constructor_callers() -> None:
    app_root = Path(__file__).parents[1] / "app"
    violations = [
        str(path.relative_to(app_root))
        for path in app_root.rglob("*.py")
        if path.name != "demo_broker.py" and "OKXDemoBroker(" in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_authorization_issuer_is_only_used_by_controlled_service() -> None:
    app_root = Path(__file__).parents[1] / "app"
    allowed = {
        app_root / "execution" / "demo_write_authorization.py",
        app_root / "services" / "controlled_demo_write.py",
    }
    violations = [
        str(path.relative_to(app_root))
        for path in app_root.rglob("*.py")
        if path not in allowed
        and (
            "_issue_demo_write_authorization" in path.read_text(encoding="utf-8")
            or "_ISSUER" in path.read_text(encoding="utf-8")
        )
    ]
    assert violations == []
