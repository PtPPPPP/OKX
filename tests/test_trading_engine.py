from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.config.settings import TradingMode
from app.domain.events import EventBus
from app.domain.market import Instrument
from app.domain.position import Portfolio
from app.execution.read_only_broker import ReadOnlyBroker
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.risk.risk_manager import default_risk_manager
from app.runtime.clock import BacktestClock
from app.strategies.registry import create_strategy
from app.trading_engine import (
    EngineRiskState,
    ProtectiveExitPolicy,
    TradingEngine,
)
from tests.conftest import make_candles


def test_observe_path_runs_sizing_and_risk_without_submitting(
    btc_instrument: Instrument,
) -> None:
    candle = make_candles(["100"])[0]
    clock = BacktestClock(candle.timestamp + timedelta(minutes=5))
    portfolio = Portfolio(
        {"BTC": Decimal("0"), "USDT": Decimal("100")},
        {"BTC-USDT": Decimal("0")},
    )
    engine = TradingEngine(
        run_id="observe-run",
        mode=TradingMode.DEMO,
        bar="5m",
        instrument=btc_instrument,
        strategy=create_strategy("buy_and_hold", {}, btc_instrument),
        position_sizer=FixedNotionalPositionSizer(Decimal("20")),
        risk_manager=default_risk_manager(
            maximum_order_notional=Decimal("20"),
            maximum_exposure=Decimal("100"),
            maximum_daily_loss=Decimal("10"),
            maximum_drawdown_pct=Decimal("5"),
            maximum_orders_per_minute=2,
            stale_after_seconds=600,
        ),
        broker=ReadOnlyBroker(),
        portfolio=portfolio,
        clock=clock,
        event_bus=EventBus(),
        protective_exits=ProtectiveExitPolicy(
            enabled=True,
            stop_loss_pct=Decimal("1"),
            take_profit_pct=Decimal("2"),
        ),
    )
    engine.start()
    signal = engine.generate_signals(candle)[0]
    execution = engine.execute_signal(
        signal,
        candle,
        execution_price=candle.close,
        risk_state=EngineRiskState(daily_pnl=Decimal("0"), drawdown_pct=Decimal("0")),
        submit_orders=False,
    )
    engine.stop()

    assert execution is not None
    assert execution.position_size.notional <= Decimal("20")
    assert execution.risk_decision.allowed
    assert execution.order is None
    assert engine.approved_order_count == 1
    assert engine.submitted_order_count == 0
