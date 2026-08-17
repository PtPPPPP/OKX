from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.position import Portfolio
from app.domain.signal import SignalAction
from app.runtime.clock import BacktestClock
from app.strategies.vwap_mean_reversion import (
    VWAPMeanReversionParameters,
    VWAPMeanReversionStrategy,
    _atr,
    _rsi,
    _vwap,
)


def _context(instrument: Instrument, portfolio: Portfolio, bar: Candle) -> StrategyContext:
    clock = BacktestClock(bar.timestamp + timedelta(hours=1))
    return StrategyContext(
        run_id="vwap-test",
        mode=TradingMode.BACKTEST,
        strategy_name="vwap_mean_reversion",
        instrument=instrument,
        bar="1h",
        portfolio_snapshot=portfolio.snapshot(),
        market_snapshot=MarketSnapshot(bar, bar.close),
        clock=clock,
    )


def _bars(prices: list[str], volume: str = "10") -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            start + timedelta(hours=i),
            Decimal(p),
            Decimal(p) + 1,
            Decimal(p) - 1,
            Decimal(p),
            Decimal(volume),
            True,
        )
        for i, p in enumerate(prices)
    ]


def test_vwap_math_and_window_boundary() -> None:
    bars = _bars(["10", "11", "12"])
    assert _vwap(__import__("collections").deque(bars), 3) == Decimal("11")
    assert _vwap(__import__("collections").deque(bars), 4) is None


def test_rsi_and_atr_are_finite() -> None:
    bars = _bars([str(100 + (i % 3)) for i in range(16)])
    from collections import deque

    assert _rsi(deque(bars), 14) is not None
    assert _atr(deque(bars), 14) is not None


def test_warmup_and_zero_volume_are_safe(btc_instrument: Instrument) -> None:
    strategy = VWAPMeanReversionStrategy(VWAPMeanReversionParameters())
    bars = _bars(["100"] * 24)
    strategy.on_start(_context(btc_instrument, Portfolio({"USDT": Decimal("1000")}), bars[0]))
    assert (
        strategy.on_bar(
            _context(btc_instrument, Portfolio({"USDT": Decimal("1000")}), bars[0]), bars[0]
        )[0].action
        is SignalAction.HOLD
    )
    bad = Candle(
        bars[-1].timestamp + timedelta(hours=1),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        Decimal("0"),
        True,
    )
    assert (
        strategy.on_bar(_context(btc_instrument, Portfolio({"USDT": Decimal("1000")}), bad), bad)[
            0
        ].action
        is SignalAction.HOLD
    )


def test_entry_signal_and_duplicate_idempotency(btc_instrument: Instrument) -> None:
    strategy = VWAPMeanReversionStrategy(VWAPMeanReversionParameters())
    values = ["100"] * 14 + ["98.5"] * 10
    portfolio = Portfolio({"USDT": Decimal("1000")})
    strategy.on_start(_context(btc_instrument, portfolio, _bars(["100"])[0]))
    signals = [
        strategy.on_bar(_context(btc_instrument, portfolio, bar), bar)[0] for bar in _bars(values)
    ]
    assert signals[-1].action is SignalAction.BUY
    assert (
        strategy.on_bar(_context(btc_instrument, portfolio, _bars(values)[-1]), _bars(values)[-1])[
            0
        ].action
        is SignalAction.HOLD
    )


def test_position_state_snapshot_restore() -> None:
    strategy = VWAPMeanReversionStrategy(VWAPMeanReversionParameters())
    strategy.state.entry_price = Decimal("100")
    strategy.state.stop_price = Decimal("98")
    strategy.state.entry_candle_index = 24
    strategy.state.holding_bars = 4
    restored = VWAPMeanReversionStrategy(VWAPMeanReversionParameters())
    restored.restore_state(strategy.state_snapshot())
    assert restored.state.snapshot() == strategy.state.snapshot()
