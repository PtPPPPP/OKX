from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from math import pow
from typing import Any, Protocol

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.events import EventBus
from app.domain.market import Instrument
from app.domain.order import OrderSide
from app.domain.position import Portfolio
from app.domain.signal import Signal, SignalAction
from app.execution.backtest_broker import Fill
from app.execution.base import Broker
from app.market.historical_data import BAR_INTERVALS
from app.market.providers import MarketDataProvider
from app.position_sizing.base import PositionSizer
from app.risk.risk_manager import RiskManager
from app.runtime.clock import BacktestClock
from app.strategies.base import Strategy
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
        self.run_id = run_id
        self.config = config
        self.instrument = instrument
        self.provider = provider
        self.strategy = strategy
        self.position_sizer = position_sizer
        self.risk_manager = risk_manager
        self.broker = broker
        self.clock = clock
        self.event_bus = event_bus
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
                enabled=config.protective_exits.enabled,
                stop_loss_pct=config.protective_exits.stop_loss_pct,
                take_profit_pct=config.protective_exits.take_profit_pct,
            ),
            repository=repository,
        )

    def run(self) -> BacktestResult:
        candles = self.provider.get_historical_bars(
            self.instrument.instrument_id,
            self.config.market.bar,
            limit=self.config.data.limit,
        )
        if not candles:
            raise ValueError("回测没有可用 K 线")
        if any(not candle.confirmed for candle in candles):
            raise ValueError("回测数据只能包含已确认收盘 K 线")
        interval = BAR_INTERVALS.get(self.config.market.bar.lower())
        if interval is None:
            raise ValueError(f"不支持的 K 线周期: {self.config.market.bar}")

        started_at = candles[0].timestamp
        self.clock.advance_to(started_at)
        self.trading_engine.start()
        pending_signals: list[Signal] = []
        equity_curve: list[EquityPoint] = []
        peak_equity = self.config.backtest.initial_capital
        daily_realized: dict[date, Decimal] = {}

        for candle in candles:
            self.clock.advance_to(candle.timestamp)
            if pending_signals:
                before = self.broker.portfolio.realized_pnl
                for signal in pending_signals:
                    equity = self.broker.portfolio.equity(
                        self.instrument.instrument_id,
                        self.instrument.base_currency,
                        self.instrument.quote_currency,
                        candle.open,
                    )
                    drawdown = (
                        (peak_equity - equity) / peak_equity * Decimal("100")
                        if peak_equity > 0
                        else Decimal("0")
                    )
                    self.trading_engine.execute_signal(
                        signal,
                        candle,
                        execution_price=candle.open,
                        risk_state=EngineRiskState(
                            daily_pnl=daily_realized.get(candle.timestamp.date(), Decimal("0")),
                            drawdown_pct=drawdown,
                        ),
                        submit_orders=True,
                    )
                change = self.broker.portfolio.realized_pnl - before
                if change:
                    trade_date = candle.timestamp.date()
                    daily_realized[trade_date] = (
                        daily_realized.get(trade_date, Decimal("0")) + change
                    )
                pending_signals = []

            equity = self.broker.portfolio.equity(
                self.instrument.instrument_id,
                self.instrument.base_currency,
                self.instrument.quote_currency,
                candle.close,
            )
            peak_equity = max(peak_equity, equity)
            equity_curve.append(
                EquityPoint(
                    run_id=self.run_id,
                    strategy_name=self.strategy.name,
                    instrument_id=self.instrument.instrument_id,
                    bar=self.config.market.bar,
                    timestamp=candle.timestamp,
                    equity=equity,
                    quote_balance=self.broker.portfolio.balances.get(
                        self.instrument.quote_currency, Decimal("0")
                    ),
                    base_quantity=self.broker.portfolio.positions.get(
                        self.instrument.instrument_id, Decimal("0")
                    ),
                    mark_price=candle.close,
                )
            )

            self.clock.advance_to(candle.timestamp + interval)
            signals = self.trading_engine.generate_signals(candle)
            pending_signals.extend(
                signal for signal in signals if signal.action is not SignalAction.HOLD
            )

        self.trading_engine.stop()
        completed_at = self.clock.now()
        final_equity = equity_curve[-1].equity
        summary = _summary(
            started_at,
            completed_at,
            self.config.backtest.initial_capital,
            final_equity,
            self.broker.fills,
            equity_curve,
            self.broker.closed_trade_pnls,
            self.broker.portfolio.positions.get(self.instrument.instrument_id, Decimal("0")) > 0,
            signal_count=self.trading_engine.signal_count,
            actionable_signal_count=self.trading_engine.actionable_signal_count,
            approved_order_count=self.trading_engine.approved_order_count,
            submitted_order_count=self.trading_engine.submitted_order_count,
        )
        return BacktestResult(
            run_id=self.run_id,
            mode=TradingMode.BACKTEST.value,
            strategy_name=self.strategy.name,
            instrument_id=self.instrument.instrument_id,
            bar=self.config.market.bar,
            started_at=started_at,
            completed_at=completed_at,
            initial_capital=self.config.backtest.initial_capital,
            final_equity=final_equity,
            fills=tuple(self.broker.fills),
            equity_curve=tuple(equity_curve),
            closed_trade_pnls=tuple(self.broker.closed_trade_pnls),
            summary=summary,
        )


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
) -> dict[str, Any]:
    wins = [pnl for pnl in closed_pnls if pnl > 0]
    losses = [pnl for pnl in closed_pnls if pnl < 0]
    gross_profit = sum(wins, start=Decimal("0"))
    gross_loss = abs(sum(losses, start=Decimal("0")))
    duration_seconds = (completed_at - started_at).total_seconds()
    periods_per_year = (
        (len(curve) - 1) * 365 * 24 * 60 * 60 / duration_seconds
        if duration_seconds > 0 and len(curve) > 1
        else 0.0
    )
    returns = [
        (current.equity - previous.equity) / previous.equity
        for previous, current in pairwise(curve)
        if previous.equity > 0
    ]
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
        "maximum_drawdown_pct": maximum_drawdown([point.equity for point in curve]),
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
        "profit_loss_ratio": gross_profit / gross_loss if gross_loss else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_holding_time_hours": None,
        "exit_reason_percentages": {},
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
) -> Decimal:
    duration_seconds = (completed_at - started_at).total_seconds()
    if initial_capital <= 0 or final_equity <= 0 or duration_seconds <= 0:
        return Decimal("0")
    years = duration_seconds / (365 * 24 * 60 * 60)
    value = (pow(float(final_equity / initial_capital), 1 / years) - 1) * 100
    return Decimal(str(value))
