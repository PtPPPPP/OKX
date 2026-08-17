from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Instrument
from app.domain.position import Portfolio
from app.domain.signal import SignalAction
from app.runtime.clock import BacktestClock
from app.strategies.registry import create_strategy, strategy_descriptions
from tests.conftest import make_candles


def context(instrument: Instrument, strategy_name: str, price: Decimal) -> StrategyContext:
    candle = make_candles([str(price)])[0]
    clock = BacktestClock(candle.timestamp + timedelta(minutes=5))
    portfolio = Portfolio({instrument.quote_currency: Decimal("10000")})
    return StrategyContext(
        run_id="run",
        mode=TradingMode.BACKTEST,
        strategy_name=strategy_name,
        instrument=instrument,
        bar="5m",
        portfolio_snapshot=portfolio.snapshot(),
        market_snapshot=MarketSnapshot(candle, price),
        clock=clock,
    )


def test_registry_exposes_two_replaceable_strategies() -> None:
    names = {item["name"] for item in strategy_descriptions()}
    assert names == {"moving_average_cross", "buy_and_hold", "vwap_mean_reversion"}


def test_buy_and_hold_buys_once(btc_instrument: Instrument) -> None:
    strategy = create_strategy("buy_and_hold", {}, btc_instrument)
    candles = make_candles(["100", "101"])
    ctx = context(btc_instrument, strategy.name, Decimal("100"))
    strategy.on_start(ctx)
    assert strategy.on_bar(ctx, candles[0])[0].action is SignalAction.BUY
    assert strategy.on_bar(ctx, candles[1])[0].action is SignalAction.HOLD


def test_moving_average_cross_is_instrument_agnostic(eth_instrument: Instrument) -> None:
    strategy = create_strategy(
        "moving_average_cross", {"fast_period": 2, "slow_period": 3}, eth_instrument
    )
    candles = make_candles(["3", "2", "1", "4"])
    ctx = context(eth_instrument, strategy.name, Decimal("4"))
    strategy.on_start(ctx)
    signals = [strategy.on_bar(ctx, candle)[0] for candle in candles]
    assert signals[-1].action is SignalAction.BUY
    assert signals[-1].instrument_id == "ETH-USDT"


def test_unregistered_strategy_has_clear_error(btc_instrument: Instrument) -> None:
    with pytest.raises(ValueError, match="未注册策略"):
        create_strategy("missing", {}, btc_instrument)
