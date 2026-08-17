from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.config.run_config import RunConfig
from app.domain.events import EventBus
from app.domain.market import Candle, Instrument
from app.domain.position import Portfolio
from app.execution.backtest_broker import BacktestBroker
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.risk.risk_manager import default_risk_manager
from app.runtime.clock import BacktestClock
from app.strategies.registry import create_strategy
from backtest.engine import BacktestEngine, BacktestResult
from backtest.metrics import maximum_drawdown
from tests.conftest import make_candles


class MemoryProvider:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        return self.candles[-limit:] if limit is not None else self.candles


def run_engine(
    instrument: Instrument,
    strategy_name: str,
    parameters: dict[str, object],
    candles: list[Candle],
) -> BacktestResult:
    config = RunConfig.model_validate(
        {
            "market": {
                "instrument_id": instrument.instrument_id,
                "instrument_type": "spot",
                "bar": "5m",
            },
            "strategy": {"name": strategy_name, "parameters": parameters},
            "data": {"source": "okx", "limit": len(candles)},
        }
    )
    portfolio = Portfolio(
        {instrument.quote_currency: config.backtest.initial_capital},
        {instrument.instrument_id: Decimal("0")},
    )
    broker = BacktestBroker(
        portfolio,
        instrument,
        config.backtest.fee_rate,
        config.backtest.slippage_rate,
    )
    manager = default_risk_manager(
        maximum_order_notional=config.risk.max_order_notional,
        maximum_exposure=config.risk.max_total_exposure,
        maximum_daily_loss=config.risk.max_daily_loss,
        maximum_drawdown_pct=config.risk.max_drawdown_pct,
        maximum_orders_per_minute=config.risk.max_orders_per_minute,
        stale_after_seconds=config.risk.stale_after_seconds,
    )
    return BacktestEngine(
        run_id="run",
        config=config,
        instrument=instrument,
        provider=MemoryProvider(candles),
        strategy=create_strategy(strategy_name, parameters, instrument),
        position_sizer=FixedNotionalPositionSizer(Decimal("20")),
        risk_manager=manager,
        broker=broker,
        clock=BacktestClock(candles[0].timestamp),
        event_bus=EventBus(),
    ).run()


def test_same_engine_runs_moving_average_and_buy_hold(
    btc_instrument: Instrument, eth_instrument: Instrument
) -> None:
    ma = run_engine(
        btc_instrument,
        "moving_average_cross",
        {"fast_period": 2, "slow_period": 3},
        make_candles(["3", "2", "1", "4", "4"]),
    )
    hold = run_engine(
        eth_instrument,
        "buy_and_hold",
        {},
        make_candles(["100", "100", "100"]),
    )
    assert ma.strategy_name == "moving_average_cross"
    assert hold.strategy_name == "buy_and_hold"
    assert ma.instrument_id == "BTC-USDT"
    assert hold.instrument_id == "ETH-USDT"


def test_signal_fills_at_next_bar_open(btc_instrument: Instrument) -> None:
    candles = make_candles(["100", "100", "100"])
    result = run_engine(btc_instrument, "buy_and_hold", {}, candles)
    assert result.fills[0].timestamp == candles[1].timestamp
    assert result.fills[0].reference_price == candles[1].open


def test_last_bar_signal_is_not_filled(btc_instrument: Instrument) -> None:
    result = run_engine(btc_instrument, "buy_and_hold", {}, make_candles(["100"]))
    assert result.fills == ()


def test_fee_slippage_and_non_negative_portfolio(btc_instrument: Instrument) -> None:
    result = run_engine(btc_instrument, "buy_and_hold", {}, make_candles(["100", "100", "100"]))
    fill = result.fills[0]
    assert fill.fill_price == fill.reference_price * Decimal("1.0005")
    assert fill.fee == fill.notional * Decimal("0.001")
    assert all(point.quote_balance >= 0 for point in result.equity_curve)
    assert all(point.base_quantity >= 0 for point in result.equity_curve)


def test_equity_curve_dimensions_are_recorded(btc_instrument: Instrument) -> None:
    result = run_engine(btc_instrument, "buy_and_hold", {}, make_candles(["100", "100", "100"]))
    point = result.equity_curve[0]
    assert point.run_id == "run"
    assert point.strategy_name == "buy_and_hold"
    assert point.instrument_id == "BTC-USDT"
    assert point.bar == "5m"


def test_maximum_drawdown() -> None:
    assert maximum_drawdown([Decimal("100"), Decimal("120"), Decimal("90")]) == Decimal("25.00")


def test_same_input_produces_same_financial_summary(
    btc_instrument: Instrument,
) -> None:
    candles = make_candles(["100", "99", "101", "102"])
    first = run_engine(btc_instrument, "buy_and_hold", {}, candles)
    second = run_engine(btc_instrument, "buy_and_hold", {}, candles)
    assert first.summary == second.summary
    assert [(fill.side, fill.quantity, fill.fill_price, fill.fee) for fill in first.fills] == [
        (fill.side, fill.quantity, fill.fill_price, fill.fee) for fill in second.fills
    ]


def test_metrics_distinguish_fills_closed_trades_and_open_position(
    btc_instrument: Instrument,
) -> None:
    result = run_engine(btc_instrument, "buy_and_hold", {}, make_candles(["100", "100", "100"]))
    assert result.summary["signal_count"] == 3
    assert result.summary["submitted_order_count"] == 1
    assert result.summary["fill_count"] == 1
    assert result.summary["closed_trade_count"] == 0
    assert result.summary["trade_count"] == 0
    assert result.summary["open_position_count"] == 1
    assert result.summary["win_rate_pct"] is None
    assert result.summary["profit_loss_ratio"] is None
