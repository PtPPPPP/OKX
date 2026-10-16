from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from itertools import pairwise
from math import isfinite, pow
from typing import Any, Protocol

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.events import EventBus
from app.domain.market import Candle, Instrument
from app.domain.order import Order, OrderSide, OrderType
from app.domain.position import Portfolio
from app.domain.signal import Signal, SignalAction
from app.execution.backtest_broker import ClosedTrade, Fill
from app.execution.base import Broker
from app.market.historical_data import BAR_INTERVALS, normalize_candles
from app.market.providers import MarketDataProvider
from app.position_sizing.base import PositionSizer
from app.risk.risk_manager import RiskManager
from app.runtime.clock import BacktestClock
from app.strategies.base import ProtectiveExitProvider, Strategy
from app.trading_engine import (
    EngineRepository,
    EngineRiskState,
    ProtectiveExitPolicy,
    TradingEngine,
)
from backtest.metrics import maximum_drawdown, sharpe_ratio


class BacktestExecutionBroker(Broker, Protocol):
    portfolio: Portfolio
    fills: list[Fill]
    closed_trade_pnls: list[Decimal]
    closed_trades: list[ClosedTrade]

    def register_signal(self, signal: Signal) -> None: ...
    def begin_bar(self, candle: Candle) -> None: ...
    def begin_intrabar(self, at: datetime) -> None: ...
    def match_orders(self) -> list[Order]: ...

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class EquityPoint:
    run_id: str
    strategy_name: str
    instrument_id: str
    bar: str
    timestamp: datetime
    equity: Decimal
    quote_balance: Decimal
    base_quantity: Decimal
    mark_price: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    mode: str
    strategy_name: str
    instrument_id: str
    bar: str
    started_at: datetime
    completed_at: datetime
    initial_capital: Decimal
    final_equity: Decimal
    fills: tuple[Fill, ...]
    equity_curve: tuple[EquityPoint, ...]
    closed_trade_pnls: tuple[Decimal, ...]
    summary: dict[str, Any]
    closed_trades: tuple[ClosedTrade, ...]


class BacktestEngine:
    """Strategy-agnostic bar-by-bar backtest orchestration."""

    def __init__(
        self,
        *,
        run_id: str,
        config: RunConfig,
        instrument: Instrument,
        provider: MarketDataProvider,
        strategy: Strategy,
        position_sizer: PositionSizer,
        risk_manager: RiskManager,
        broker: BacktestExecutionBroker,
        clock: BacktestClock,
        event_bus: EventBus,
        repository: EngineRepository | None = None,
    ) -> None:
        self.run_id, self.config, self.instrument = run_id, config, instrument
        self.provider, self.strategy, self.position_sizer = provider, strategy, position_sizer
        self.risk_manager, self.broker, self.clock = risk_manager, broker, clock
        self.event_bus = event_bus
        self._peak_equity = config.backtest.initial_capital
        self._day_start_equity = config.backtest.initial_capital
        self._recent_order_times: list[datetime] = []
        self._protective_orders: dict[str, str] = {}
        self.trading_engine = TradingEngine(
            run_id=run_id,
            mode=TradingMode.BACKTEST,
            bar=config.market.bar,
            instrument=instrument,
            strategy=strategy,
            position_sizer=position_sizer,
            risk_manager=risk_manager,
            broker=broker,
            portfolio=broker.portfolio,
            clock=clock,
            event_bus=event_bus,
            protective_exits=ProtectiveExitPolicy(
                enabled=False,  # Backtest brackets use OHLC below.
                stop_loss_pct=config.protective_exits.stop_loss_pct,
                take_profit_pct=config.protective_exits.take_profit_pct,
            ),
            repository=repository,
        )

    def run(self) -> BacktestResult:
        candles = self.provider.get_historical_bars(
            self.instrument.instrument_id, self.config.market.bar, limit=self.config.data.limit
        )
        if not candles:
            raise ValueError("回测没有可用 K 线")
        if any(not candle.confirmed for candle in candles):
            raise ValueError("回测数据只能包含已确认收盘 K 线")
        candles = normalize_candles(candles, bar=self.config.market.bar)
        interval = BAR_INTERVALS[self.config.market.bar.lower()]
        started_at = candles[0].timestamp
        self.clock.advance_to(started_at)
        pending_signals: list[tuple[Signal, Decimal]] = []
        equity_curve: list[EquityPoint] = []
        previous_equity = self.config.backtest.initial_capital
        previous_day: date | None = None
        self.trading_engine.start()
        try:
            for candle in candles:
                self.clock.advance_to(candle.timestamp)
                if candle.timestamp.date() != previous_day:
                    self._day_start_equity = previous_equity
                    previous_day = candle.timestamp.date()
                self.broker.begin_bar(candle)
                state = self._risk_state(candle.open)
                if (
                    state.daily_pnl is not None
                    and state.daily_pnl <= -self.config.risk.max_daily_loss
                ) or (
                    state.drawdown_pct is not None
                    and state.drawdown_pct >= self.config.risk.max_drawdown_pct
                ):
                    self._cancel_orders(candle, side=OrderSide.BUY)
                protective = self._protective_exit(candle, intrabar=False)
                self._notify_matches(candle, candle.open)
                if not protective:
                    for signal, limit_price in pending_signals:
                        self._execute(signal, candle, candle.open, limit_price=limit_price)
                pending_signals.clear()
                # Already-known brackets can trigger inside this bar. An entry
                # with unknown intrabar fill time is not protected retroactively.
                self.clock.advance_to(candle.timestamp + interval)
                self.broker.begin_intrabar(self.clock.now())
                protective = self._protective_exit(candle, intrabar=True) or protective
                self._notify_matches(candle, candle.close)
                equity = self._equity(candle.close)
                self._peak_equity = max(self._peak_equity, equity)
                equity_curve.append(
                    EquityPoint(
                        self.run_id,
                        self.strategy.name,
                        self.instrument.instrument_id,
                        self.config.market.bar,
                        self.clock.now(),
                        equity,
                        self.broker.portfolio.balances.get(
                            self.instrument.quote_currency, Decimal("0")
                        ),
                        self.broker.portfolio.positions.get(
                            self.instrument.instrument_id, Decimal("0")
                        ),
                        candle.close,
                    )
                )
                previous_equity = equity
                signals = self.trading_engine.generate_signals(candle)
                if not protective:
                    pending_signals.extend(
                        (signal, candle.close)
                        for signal in signals
                        if signal.action is not SignalAction.HOLD
                    )
            pending_at_end = len(self.broker.get_open_orders())
            self._cancel_orders(candles[-1])
        finally:
            self.trading_engine.stop()
        final_equity = equity_curve[-1].equity
        quantity = self.broker.portfolio.positions.get(self.instrument.instrument_id, Decimal("0"))
        summary = _summary(
            started_at,
            self.clock.now(),
            self.config.backtest.initial_capital,
            final_equity,
            self.broker.fills,
            equity_curve,
            self.broker.closed_trade_pnls,
            quantity > 0,
            signal_count=self.trading_engine.signal_count,
            actionable_signal_count=self.trading_engine.actionable_signal_count,
            approved_order_count=self.trading_engine.approved_order_count,
            submitted_order_count=self.trading_engine.submitted_order_count,
            closed_trades=self.broker.closed_trades,
            realized_pnl=self.broker.portfolio.realized_pnl,
            unrealized_pnl=self.broker.unrealized_pnl(candles[-1].close),
            pending_at_end=pending_at_end,
        )
        return BacktestResult(
            run_id=self.run_id,
            mode=TradingMode.BACKTEST.value,
            strategy_name=self.strategy.name,
            instrument_id=self.instrument.instrument_id,
            bar=self.config.market.bar,
            started_at=started_at,
            completed_at=self.clock.now(),
            initial_capital=self.config.backtest.initial_capital,
            final_equity=final_equity,
            fills=tuple(self.broker.fills),
            equity_curve=tuple(equity_curve),
            closed_trade_pnls=tuple(self.broker.closed_trade_pnls),
            summary=summary,
            closed_trades=tuple(self.broker.closed_trades),
        )

    def _equity(self, price: Decimal) -> Decimal:
        return self.broker.portfolio.equity(
            self.instrument.instrument_id,
            self.instrument.base_currency,
            self.instrument.quote_currency,
            price,
        )

    def _risk_state(self, price: Decimal) -> EngineRiskState:
        equity = self._equity(price)
        self._peak_equity = max(self._peak_equity, equity)
        return EngineRiskState(
            daily_pnl=equity - self._day_start_equity,
            drawdown_pct=(self._peak_equity - equity) / self._peak_equity * Decimal("100")
            if self._peak_equity > 0
            else Decimal("0"),
            recent_order_times=tuple(self._recent_order_times),
        )

    def _execute(
        self,
        signal: Signal,
        candle: Candle,
        price: Decimal,
        *,
        limit_price: Decimal | None = None,
        order_type: OrderType = OrderType.LIMIT,
    ) -> Order | None:
        self.broker.register_signal(signal)
        execution = self.trading_engine.execute_signal(
            signal,
            candle,
            execution_price=price,
            risk_state=self._risk_state(price),
            submit_orders=True,
            limit_price=limit_price,
            order_type=order_type,
        )
        order = execution.order if execution is not None else None
        if order is not None:
            self._recent_order_times.append(self.clock.now())
        return order

    def _notify_matches(self, candle: Candle, price: Decimal) -> None:
        for order in self.broker.match_orders():
            self.trading_engine.record_order_update(order, candle, price)

    def _cancel_orders(self, candle: Candle, *, side: OrderSide | None = None) -> None:
        for order in self.broker.get_open_orders():
            if side is None or order.request.side is side:
                cancelled = self.broker.cancel_order(
                    self.instrument.instrument_id, order.request.client_order_id
                )
                self.trading_engine.record_order_update(cancelled, candle, candle.open)

    def _protective_exit(self, candle: Candle, *, intrabar: bool) -> bool:
        policy = self.config.protective_exits
        entry = self.broker.portfolio.average_entry_prices.get(self.instrument.instrument_id)
        if entry is None:
            return False
        stop_price: Decimal | None = None
        take_price: Decimal | None = None
        take_reason = "take_profit"
        if isinstance(self.strategy, ProtectiveExitProvider):
            levels = self.strategy.protective_exit_levels(entry)
            stop_price, take_price = levels.stop_price, levels.take_profit_price
            take_reason = levels.take_profit_reason
        if policy.enabled:
            fixed_stop = entry * (Decimal("1") - policy.stop_loss_pct / Decimal("100"))
            fixed_take = entry * (Decimal("1") + policy.take_profit_pct / Decimal("100"))
            stop_price = max(stop_price, fixed_stop) if stop_price is not None else fixed_stop
            if take_price is None or fixed_take < take_price:
                take_price, take_reason = fixed_take, "take_profit"
        if stop_price is None and take_price is None:
            return False
        open_orders = self.broker.get_open_orders()
        if any(
            self._protective_orders.get(order.request.client_order_id) == "stop_loss"
            for order in open_orders
        ):
            return True
        tick = self.instrument.price_tick
        stop = (
            (stop_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            if stop_price is not None
            else None
        )
        take = (
            (take_price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            if take_price is not None
            else None
        )
        low = candle.low if intrabar else candle.open
        high = candle.high if intrabar else candle.open
        if stop is not None and low <= stop:
            reason, order_type = "stop_loss", OrderType.MARKET
            price = stop if intrabar else candle.open
            limit_price = None
        elif take is not None and high >= take:
            reason, order_type = take_reason, OrderType.LIMIT
            price = take if intrabar else candle.open
            limit_price = take
        else:
            return False
        if any(
            self._protective_orders.get(order.request.client_order_id) == reason
            for order in open_orders
        ):
            return True
        self._cancel_orders(candle)
        signal = Signal(
            signal_id=f"{self.run_id}:{candle.timestamp.isoformat()}:{reason}",
            strategy_name=self.strategy.name,
            instrument_id=self.instrument.instrument_id,
            action=SignalAction.CLOSE,
            timestamp=self.clock.now(),
            reason=reason,
            confidence=Decimal("1"),
            metadata={"protective_exit": True, "exit_reason": reason, "candle_confirmed": True},
        )
        self.trading_engine.record_signal(signal)
        order = self._execute(signal, candle, price, limit_price=limit_price, order_type=order_type)
        if order is not None:
            self._protective_orders[order.request.client_order_id] = reason
        return True


def _summary(
    started_at: datetime,
    completed_at: datetime,
    initial_capital: Decimal,
    final_equity: Decimal,
    fills: list[Fill],
    curve: list[EquityPoint],
    closed_pnls: list[Decimal],
    has_open_position: bool,
    *,
    signal_count: int,
    actionable_signal_count: int,
    approved_order_count: int,
    submitted_order_count: int,
    closed_trades: list[ClosedTrade],
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    pending_at_end: int,
) -> dict[str, Any]:
    wins = [pnl for pnl in closed_pnls if pnl > 0]
    losses = [pnl for pnl in closed_pnls if pnl < 0]
    gross_profit = sum(wins, start=Decimal("0"))
    gross_loss = abs(sum(losses, start=Decimal("0")))
    duration_seconds = (completed_at - started_at).total_seconds()
    periods_per_year = (
        len(curve) * 365 * 24 * 60 * 60 / duration_seconds if duration_seconds > 0 else 0.0
    )
    equities = [initial_capital, *[point.equity for point in curve]]
    returns = [
        (current - previous) / previous for previous, current in pairwise(equities) if previous > 0
    ]
    exit_counts = Counter(trade.exit_reason for trade in closed_trades)
    average_win = gross_profit / Decimal(len(wins)) if wins else None
    average_loss = gross_loss / Decimal(len(losses)) if losses else None
    return {
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "total_return_pct": (final_equity - initial_capital) / initial_capital * Decimal("100")
        if initial_capital
        else Decimal("0"),
        "annualized_return_pct": _annualized_return(
            initial_capital, final_equity, started_at, completed_at
        ),
        "maximum_drawdown_pct": maximum_drawdown(equities),
        "sharpe_ratio": sharpe_ratio(returns, periods_per_year),
        "signal_count": signal_count,
        "actionable_signal_count": actionable_signal_count,
        "approved_order_count": approved_order_count,
        "submitted_order_count": submitted_order_count,
        "fill_count": len(fills),
        "closed_trade_count": len(closed_pnls),
        "winning_trade_count": len(wins),
        "losing_trade_count": len(losses),
        "open_position_count": int(has_open_position),
        "trade_count": len(closed_pnls),
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(closed_pnls)) * Decimal("100")
        if closed_pnls
        else None,
        "profit_loss_ratio": average_win / average_loss
        if average_win is not None and average_loss
        else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_holding_time_hours": sum(
            (trade.holding_time_hours for trade in closed_trades), start=Decimal("0")
        )
        / Decimal(len(closed_trades))
        if closed_trades
        else None,
        "exit_reason_percentages": {
            reason: Decimal(count) / Decimal(len(closed_trades)) * Decimal("100")
            for reason, count in sorted(exit_counts.items())
        },
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "net_pnl": final_equity - initial_capital,
        "pending_order_count_at_end": pending_at_end,
        "execution_model": "ohlc_limit_v2",
        "total_fees": sum((fill.fee for fill in fills), start=Decimal("0")),
        "slippage_cost": sum((fill.slippage_cost for fill in fills), start=Decimal("0")),
        "max_consecutive_losses": _max_consecutive_losses(closed_pnls),
        "buy_count": sum(fill.side is OrderSide.BUY for fill in fills),
        "sell_count": sum(fill.side is OrderSide.SELL for fill in fills),
        "has_open_position": has_open_position,
    }


def _max_consecutive_losses(pnls: list[Decimal]) -> int:
    maximum = current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _annualized_return(
    initial_capital: Decimal,
    final_equity: Decimal,
    started_at: datetime,
    completed_at: datetime,
) -> Decimal | None:
    duration_seconds = (completed_at - started_at).total_seconds()
    if initial_capital <= 0 or duration_seconds <= 0:
        return None
    if final_equity == 0:
        return Decimal("-100")
    years = duration_seconds / (365 * 24 * 60 * 60)
    try:
        value = (pow(float(final_equity / initial_capital), 1 / years) - 1) * 100
    except OverflowError:
        return None
    return Decimal(str(value)) if isfinite(value) else None
