from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType
from app.domain.position import Portfolio
from app.domain.signal import SignalAction
from app.position_sizing.fixed_notional import (
    PercentEquityPositionSizer,
    RiskPerTradePositionSizer,
    create_position_sizer,
)
from app.runtime.clock import BacktestClock
from app.strategies.ma_bollinger import MABollingerParameters, MABollingerPullbackStrategy
from app.strategies.registry import create_strategy
from tests.conftest import make_candles


def _make_context(
    instrument: Instrument,
    candle: Candle,
    *,
    position_qty: Decimal = Decimal("0"),
    average_entry_price: Decimal | None = None,
) -> StrategyContext:
    clock = BacktestClock(candle.timestamp + timedelta(hours=1))
    positions: dict[str, Decimal] = {}
    avg_prices: dict[str, Decimal] = {}
    if position_qty > 0:
        entry = average_entry_price if average_entry_price is not None else candle.close
        positions[instrument.instrument_id] = position_qty
        avg_prices[instrument.instrument_id] = entry
    portfolio = Portfolio(
        balances={instrument.quote_currency: Decimal("10000")},
        positions=positions,
        average_entry_prices=avg_prices,
    )
    return StrategyContext(
        run_id="test-ma-bb",
        mode=TradingMode.BACKTEST,
        strategy_name="ma_bollinger_pullback",
        instrument=instrument,
        bar="1h",
        portfolio_snapshot=portfolio.snapshot(),
        market_snapshot=MarketSnapshot(candle, candle.close),
        clock=clock,
    )


def _make_order(
    instrument: Instrument,
    *,
    side: OrderSide,
    state: OrderState,
    price: Decimal,
    quantity: Decimal = Decimal("0.1"),
) -> Order:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = OrderRequest(
        client_order_id="cl-1",
        signal_id="sig-1",
        instrument_id=instrument.instrument_id,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        created_at=now,
    )
    filled = quantity if state is OrderState.FILLED else Decimal("0")
    return Order(
        request=request,
        state=state,
        updated_at=now,
        filled_quantity=filled,
        average_price=price if state is OrderState.FILLED else None,
    )


def test_parameters_validation() -> None:
    params = MABollingerParameters()
    assert params.trend_ma_period == 50
    assert params.bollinger_period == 20
    assert params.bollinger_std_dev == Decimal("2")

    with pytest.raises(ValidationError):
        MABollingerParameters(trend_ma_period=10, bollinger_period=20)

    with pytest.raises(ValidationError):
        MABollingerParameters(bollinger_std_dev=Decimal("NaN"))

    with pytest.raises(ValidationError):
        MABollingerParameters(unknown_field=1)  # type: ignore[call-arg]


def test_ignores_unconfirmed_and_duplicate_bars(btc_instrument: Instrument) -> None:
    strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=4,
            bollinger_period=3,
            require_trend_rising=False,
        )
    )
    candles = make_candles(["100", "101", "102", "103"])
    unconfirmed = replace(candles[0], confirmed=False)
    ctx = _make_context(btc_instrument, unconfirmed)
    strategy.on_start(ctx)

    sig_unconfirmed = strategy.on_bar(ctx, unconfirmed)[0]
    assert sig_unconfirmed.action is SignalAction.HOLD
    assert "未确认" in sig_unconfirmed.reason

    first_sig = strategy.on_bar(ctx, candles[0])[0]
    assert first_sig.action is SignalAction.HOLD
    dup_sig = strategy.on_bar(ctx, candles[0])[0]
    assert dup_sig.action is SignalAction.HOLD
    assert "重复" in dup_sig.reason


def test_buys_on_uptrend_bollinger_pullback_and_exits_at_middle(btc_instrument: Instrument) -> None:
    strategy = create_strategy(
        "ma_bollinger_pullback",
        {
            "trend_ma_period": 6,
            "trend_ma_type": "sma",
            "bollinger_period": 4,
            "bollinger_std_dev": "1.5",
            "min_bandwidth_pct": "0.001",
            "require_trend_rising": True,
            "exit_band": "middle",
            "exit_on_trend_break": True,
            "stop_loss_pct": "0.10",
            "max_hold_bars": 20,
        },
        btc_instrument,
    )
    # Rising prices establish an upward trend MA, then a pullback hits the lower band while still >= trend MA.
    prices = ["100", "102", "104", "110", "122", "125", "126", "118", "124"]
    candles = make_candles(prices)
    strategy.on_start(_make_context(btc_instrument, candles[0]))

    buy_signal = None
    for candle in candles[:8]:
        ctx = _make_context(btc_instrument, candle)
        signal = strategy.on_bar(ctx, candle)[0]
        if signal.action is SignalAction.BUY:
            buy_signal = signal

    assert buy_signal is not None
    assert "trend_ma" in buy_signal.metadata
    assert "bb_lower" in buy_signal.metadata
    assert Decimal(buy_signal.metadata["bb_lower"]) >= Decimal("118")
    assert Decimal("118") >= Decimal(buy_signal.metadata["trend_ma"])

    # Simulate filled buy and verify middle-band take profit on rebound.
    fill_order = _make_order(
        btc_instrument,
        side=OrderSide.BUY,
        state=OrderState.FILLED,
        price=Decimal("118"),
    )
    rebound_ctx = _make_context(
        btc_instrument,
        candles[8],
        position_qty=Decimal("0.1"),
        average_entry_price=Decimal("118"),
    )
    strategy.on_order_update(rebound_ctx, fill_order)
    exit_signal = strategy.on_bar(rebound_ctx, candles[8])[0]
    assert exit_signal.action is SignalAction.CLOSE
    assert exit_signal.metadata["exit_reason"] == "bollinger_take_profit"


def test_does_not_buy_when_below_trend_ma_or_narrow_bandwidth(btc_instrument: Instrument) -> None:
    strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=5,
            bollinger_period=4,
            bollinger_std_dev=Decimal("1.0"),
            min_bandwidth_pct=Decimal("0.01"),
            require_trend_rising=True,
        )
    )
    # Downtrend: sharp drop breaks below trend MA -> must not buy.
    candles = make_candles(["120", "118", "116", "114", "112", "105", "95"])
    strategy.on_start(_make_context(btc_instrument, candles[0]))
    actions = [strategy.on_bar(_make_context(btc_instrument, c), c)[0].action for c in candles]
    assert SignalAction.BUY not in actions

    # Flat market: zero bandwidth -> filtered out by min_bandwidth_pct.
    flat_candles = make_candles(["100", "100", "100", "100", "100", "100", "100"])
    strategy.on_start(_make_context(btc_instrument, flat_candles[0]))
    flat_actions = [
        strategy.on_bar(_make_context(btc_instrument, c), c)[0].action for c in flat_candles
    ]
    assert SignalAction.BUY not in flat_actions


def test_exit_reasons_stop_loss_trend_break_and_time_stop(btc_instrument: Instrument) -> None:
    # 1. Fixed stop loss
    sl_strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=4,
            bollinger_period=4,
            require_trend_rising=False,
            stop_loss_pct=Decimal("0.05"),
            exit_on_trend_break=False,
            max_hold_bars=None,
        )
    )
    candles = make_candles(["100", "100", "100", "100", "94"])
    sl_strategy.on_start(_make_context(btc_instrument, candles[0]))
    for candle in candles[:4]:
        sl_strategy.on_bar(_make_context(btc_instrument, candle), candle)
    sl_ctx = _make_context(
        btc_instrument,
        candles[4],
        position_qty=Decimal("0.1"),
        average_entry_price=Decimal("100"),
    )
    sl_sig = sl_strategy.on_bar(sl_ctx, candles[4])[0]
    assert sl_sig.action is SignalAction.CLOSE
    assert sl_sig.metadata["exit_reason"] == "stop_loss"

    # 2. Trend break exit
    tb_strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=4,
            bollinger_period=4,
            require_trend_rising=False,
            stop_loss_pct=Decimal("0.20"),
            exit_on_trend_break=True,
            max_hold_bars=None,
        )
    )
    tb_candles = make_candles(["100", "105", "110", "115", "102"])
    tb_strategy.on_start(_make_context(btc_instrument, tb_candles[0]))
    for candle in tb_candles[:4]:
        tb_strategy.on_bar(_make_context(btc_instrument, candle), candle)
    tb_ctx = _make_context(
        btc_instrument,
        tb_candles[4],
        position_qty=Decimal("0.1"),
        average_entry_price=Decimal("105"),
    )
    tb_sig = tb_strategy.on_bar(tb_ctx, tb_candles[4])[0]
    assert tb_sig.action is SignalAction.CLOSE
    assert tb_sig.metadata["exit_reason"] == "trend_break"

    # 3. Time stop
    ts_strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=4,
            bollinger_period=4,
            require_trend_rising=False,
            stop_loss_pct=Decimal("0.20"),
            exit_band="upper",
            exit_on_trend_break=False,
            max_hold_bars=2,
        )
    )
    ts_candles = make_candles(["100", "104", "108", "112", "107", "107"])
    ts_strategy.on_start(_make_context(btc_instrument, ts_candles[0]))
    for candle in ts_candles[:4]:
        ts_strategy.on_bar(_make_context(btc_instrument, candle), candle)
    for idx in (4, 5):
        ts_ctx = _make_context(
            btc_instrument,
            ts_candles[idx],
            position_qty=Decimal("0.1"),
            average_entry_price=Decimal("108"),
        )
        ts_sig = ts_strategy.on_bar(ts_ctx, ts_candles[idx])[0]
    assert ts_sig.action is SignalAction.CLOSE
    assert ts_sig.metadata["exit_reason"] == "time_stop"


def test_ema_mode_and_active_order_guard(eth_instrument: Instrument) -> None:
    strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=5,
            trend_ma_type="ema",
            bollinger_period=4,
            bollinger_std_dev=Decimal("1.0"),
            min_bandwidth_pct=Decimal("0.001"),
            require_trend_rising=True,
        )
    )
    candles = make_candles(["100", "103", "107", "112", "118", "124", "116"])
    strategy.on_start(_make_context(eth_instrument, candles[0]))
    for candle in candles[:6]:
        strategy.on_bar(_make_context(eth_instrument, candle), candle)

    # Mark an open order before the pullback bar -> must hold instead of duplicating buy.
    open_order = _make_order(
        eth_instrument,
        side=OrderSide.BUY,
        state=OrderState.ACCEPTED,
        price=Decimal("116"),
    )
    last_ctx = _make_context(eth_instrument, candles[6])
    strategy.on_order_update(last_ctx, open_order)
    sig = strategy.on_bar(last_ctx, candles[6])[0]
    assert sig.action is SignalAction.HOLD
    assert "活动订单" in sig.reason
    assert strategy.protective_exit_levels(Decimal("100")).stop_price == Decimal("98.00")


def test_confirm_rebound_and_atr_stop_distance(btc_instrument: Instrument) -> None:
    strategy = MABollingerPullbackStrategy(
        MABollingerParameters(
            trend_ma_period=5,
            bollinger_period=4,
            bollinger_std_dev=Decimal("1.2"),
            min_bandwidth_pct=Decimal("0.001"),
            trend_tolerance_pct=Decimal("0.05"),
            require_trend_rising=False,
            confirm_rebound=True,
            atr_period=3,
            atr_stop_multiplier=Decimal("1.5"),
            stop_loss_pct=Decimal("0.10"),
        )
    )
    raw = make_candles(["100", "105", "110", "115", "120", "108", "113"])
    # Make bar 5 a bearish dip below lower band, and bar 6 a bullish rebound candle (close > open).
    candles = list(raw)
    candles[5] = replace(
        candles[5],
        open=Decimal("114"),
        high=Decimal("115"),
        low=Decimal("107"),
        close=Decimal("108"),
    )
    candles[6] = replace(
        candles[6],
        open=Decimal("108"),
        high=Decimal("114"),
        low=Decimal("108"),
        close=Decimal("113"),
    )
    strategy.on_start(_make_context(btc_instrument, candles[0]))
    signals = [strategy.on_bar(_make_context(btc_instrument, c), c)[0] for c in candles]
    # Bar 5 (the dip itself) must HOLD because confirm_rebound waits for a bullish rebound bar.
    assert signals[5].action is SignalAction.HOLD
    assert signals[6].action is SignalAction.BUY
    assert "atr" in signals[6].metadata
    assert "stop_distance_pct" in signals[6].metadata
    assert Decimal(signals[6].metadata["stop_distance_pct"]) > Decimal("0")


def test_percent_equity_and_risk_per_trade_position_sizers(btc_instrument: Instrument) -> None:
    candle = make_candles(["100"])[0]
    ctx = _make_context(btc_instrument, candle)
    buy_sig = MABollingerPullbackStrategy(MABollingerParameters())._signal(
        ctx,
        candle,
        SignalAction.BUY,
        "test",
    )
    buy_sig.metadata["stop_distance_pct"] = "0.02"

    pct_sizer = create_position_sizer("percent_equity", {"equity_pct": "0.15"})
    assert isinstance(pct_sizer, PercentEquityPositionSizer)
    pct_decision = pct_sizer.calculate(
        buy_sig, ctx.portfolio_snapshot, ctx.market_snapshot, btc_instrument
    )
    assert pct_decision.notional == Decimal("1500.00")

    risk_sizer = create_position_sizer(
        "risk_per_trade",
        {"risk_pct": "0.005", "default_stop_pct": "0.02", "max_notional_pct": "0.30"},
    )
    assert isinstance(risk_sizer, RiskPerTradePositionSizer)
    # Equity = 10000, risk_pct = 0.5% (50 USDT), stop_distance = 2% -> target notional = 2500 USDT
    risk_decision = risk_sizer.calculate(
        buy_sig, ctx.portfolio_snapshot, ctx.market_snapshot, btc_instrument
    )
    assert risk_decision.notional == Decimal("2500.00")
