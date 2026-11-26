"""Time-synchronized multi-asset portfolio backtest engine sharing one equity pool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.events import EventBus
from app.domain.market import Candle, Instrument
from app.domain.order import OrderType
from app.domain.position import Portfolio
from app.domain.signal import Signal, SignalAction
from app.execution.backtest_broker import BacktestBroker, ClosedTrade, Fill
from app.market.historical_data import BAR_INTERVALS, normalize_candles
from app.position_sizing.fixed_notional import create_position_sizer
from app.risk.risk_manager import default_risk_manager
from app.runtime.clock import BacktestClock
from app.strategies.registry import create_strategy
from app.trading_engine import EngineRiskState, ProtectiveExitPolicy, TradingEngine
from backtest.engine import EquityPoint
from backtest.metrics import maximum_drawdown, sharpe_ratio


@dataclass(frozen=True, slots=True)
class MultiAssetBacktestResult:
    run_id: str
    strategy_name: str
    instruments: tuple[str, ...]
    bar: str
    started_at: datetime
    completed_at: datetime
    initial_capital: Decimal
    final_equity: Decimal
    fills: tuple[Fill, ...]
    closed_trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    summary: dict[str, Any]
    per_instrument_summary: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _AssetSlot:
    instrument: Instrument
    broker: BacktestBroker
    engine: TradingEngine
    candles_by_ts: dict[datetime, Candle]
    pending_signals: list[tuple[Signal, Decimal]]
    last_close: Decimal | None = None


class MultiAssetBacktestEngine:
    """Synchronized bar-by-bar multi-asset backtest sharing a single Portfolio balance."""

    def __init__(
        self,
        *,
        config: RunConfig,
        instruments: dict[str, Instrument],
        candles_by_instrument: dict[str, list[Candle]],
        run_id: str | None = None,
    ) -> None:
        if not instruments:
            raise ValueError("多品种组合回测至少需要 1 个交易品种")
        self.run_id = run_id or uuid4().hex
        self.config = config
        self.instruments = instruments
        quote_currencies = {inst.quote_currency for inst in instruments.values()}
        if len(quote_currencies) != 1:
            raise ValueError("多品种组合回测要求所有品种共享同一计价货币")
        self.quote_currency = next(iter(quote_currencies))

        self.portfolio = Portfolio(
            balances={self.quote_currency: config.backtest.initial_capital},
            positions={inst_id: Decimal("0") for inst_id in instruments},
        )
        self.clock = BacktestClock(datetime.min.replace(tzinfo=UTC))
        self.event_bus = EventBus()

        self._slots: dict[str, _AssetSlot] = {}
        all_timestamps: set[datetime] = set()
        for inst_id, instrument in instruments.items():
            raw_candles = candles_by_instrument.get(inst_id, [])
            if not raw_candles:
                raise ValueError(f"品种 {inst_id} 缺少历史 K 线数据")
            normalized = normalize_candles(raw_candles, bar=config.market.bar)
            by_ts = {c.timestamp: c for c in normalized}
            all_timestamps.update(by_ts.keys())

            broker = BacktestBroker(
                self.portfolio,
                instrument,
                config.backtest.fee_rate,
                config.backtest.slippage_rate,
                volume_participation=config.backtest.volume_participation,
            )
            strategy = create_strategy(
                config.strategy.name,
                config.strategy.parameters,
                instrument,
            )
            sizer = create_position_sizer(
                config.position_sizing.name,
                config.position_sizing.parameters,
            )
            risk_manager = default_risk_manager(
                maximum_order_notional=config.risk.max_order_notional,
                maximum_exposure=config.risk.max_total_exposure,
                maximum_daily_loss=config.risk.max_daily_loss,
                maximum_drawdown_pct=config.risk.max_drawdown_pct,
                maximum_orders_per_minute=config.risk.max_orders_per_minute,
                stale_after_seconds=config.risk.stale_after_seconds,
            )
            engine = TradingEngine(
                run_id=self.run_id,
                mode=TradingMode.BACKTEST,
                bar=config.market.bar,
                instrument=instrument,
                strategy=strategy,
                position_sizer=sizer,
                risk_manager=risk_manager,
                broker=broker,
                portfolio=self.portfolio,
                clock=self.clock,
                event_bus=self.event_bus,
                protective_exits=ProtectiveExitPolicy(
                    enabled=config.protective_exits.enabled,
                    stop_loss_pct=config.protective_exits.stop_loss_pct,
                    take_profit_pct=config.protective_exits.take_profit_pct,
                ),
            )
            self._slots[inst_id] = _AssetSlot(
                instrument=instrument,
                broker=broker,
                engine=engine,
                candles_by_ts=by_ts,
                pending_signals=[],
            )
        self._timeline = sorted(all_timestamps)

    def run(self) -> MultiAssetBacktestResult:
        interval = BAR_INTERVALS[self.config.market.bar.lower()]
        for slot in self._slots.values():
            slot.engine.start()

        equity_curve: list[EquityPoint] = []
        peak_equity = self.config.backtest.initial_capital
        day_start_equity = self.config.backtest.initial_capital
        previous_day = None

        try:
            for ts in self._timeline:
                self.clock.advance_to(ts)
                if ts.date() != previous_day:
                    day_start_equity = (
                        equity_curve[-1].equity
                        if equity_curve
                        else self.config.backtest.initial_capital
                    )
                    previous_day = ts.date()

                # Phase 1: Bar open matching & executing signals generated on previous bar close
                for slot in self._slots.values():
                    candle = slot.candles_by_ts.get(ts)
                    if candle is None:
                        continue
                    slot.broker.begin_bar(candle)
                    for order in slot.broker.match_orders():
                        slot.engine.record_order_update(order, candle, candle.open)

                    cur_eq = self._portfolio_equity(
                        mark_override={slot.instrument.instrument_id: candle.open}
                    )
                    daily_pnl = cur_eq - day_start_equity
                    drawdown_pct = (
                        (peak_equity - cur_eq) / peak_equity * Decimal("100")
                        if peak_equity > 0
                        else Decimal("0")
                    )
                    risk_state = EngineRiskState(
                        daily_pnl=daily_pnl,
                        drawdown_pct=drawdown_pct,
                    )
                    for signal, limit_price in slot.pending_signals:
                        slot.broker.register_signal(signal)
                        is_protective_close = bool(
                            signal.action in {SignalAction.SELL, SignalAction.CLOSE}
                            and signal.metadata.get("protective_exit")
                        )
                        exec_result = slot.engine.execute_signal(
                            signal,
                            candle,
                            execution_price=candle.open,
                            limit_price=None if is_protective_close else limit_price,
                            order_type=OrderType.MARKET if is_protective_close else OrderType.LIMIT,
                            risk_state=risk_state,
                            submit_orders=True,
                        )
                        if exec_result is not None and exec_result.order is not None:
                            for matched in slot.broker.match_orders():
                                slot.engine.record_order_update(matched, candle, candle.open)
                    slot.pending_signals.clear()

                # Phase 2: Intrabar matching & bar close signal generation
                close_time = ts + interval
                self.clock.advance_to(close_time)
                for slot in self._slots.values():
                    candle = slot.candles_by_ts.get(ts)
                    if candle is None:
                        continue
                    slot.last_close = candle.close
                    slot.broker.begin_intrabar(close_time)
                    for order in slot.broker.match_orders():
                        slot.engine.record_order_update(order, candle, candle.close)
                    signals = slot.engine.generate_signals(candle)
                    for sig in signals:
                        if sig.action is not SignalAction.HOLD:
                            slot.pending_signals.append((sig, candle.close))

                total_eq = self._portfolio_equity()
                peak_equity = max(peak_equity, total_eq)
                equity_curve.append(
                    EquityPoint(
                        run_id=self.run_id,
                        strategy_name=self.config.strategy.name,
                        instrument_id="PORTFOLIO",
                        bar=self.config.market.bar,
                        timestamp=close_time,
                        equity=total_eq,
                        quote_balance=self.portfolio.balances.get(
                            self.quote_currency, Decimal("0")
                        ),
                        base_quantity=Decimal("0"),
                        mark_price=Decimal("1"),
                    )
                )
        finally:
            for slot in self._slots.values():
                slot.engine.stop()

        all_fills: list[Fill] = []
        all_closed: list[ClosedTrade] = []
        per_inst: dict[str, dict[str, Any]] = {}
        for inst_id, slot in self._slots.items():
            all_fills.extend(slot.broker.fills)
            all_closed.extend(slot.broker.closed_trades)
            trades = slot.broker.closed_trades
            wins = [t for t in trades if t.pnl > 0]
            net_pnl = sum((t.pnl for t in trades), Decimal("0"))
            fees = sum((f.fee for f in slot.broker.fills), Decimal("0"))
            per_inst[inst_id] = {
                "closed_trade_count": len(trades),
                "winning_trade_count": len(wins),
                "win_rate_pct": str(
                    (Decimal(len(wins)) / Decimal(len(trades)) * Decimal("100"))
                    if trades
                    else Decimal("0")
                ),
                "realized_net_pnl": str(net_pnl),
                "total_fees": str(fees),
                "ending_position": str(self.portfolio.positions.get(inst_id, Decimal("0"))),
            }

        all_fills.sort(key=lambda f: f.timestamp)
        all_closed.sort(key=lambda t: t.exited_at)
        final_equity = (
            equity_curve[-1].equity if equity_curve else self.config.backtest.initial_capital
        )
        initial = self.config.backtest.initial_capital
        total_ret_pct = (
            (final_equity - initial) / initial * Decimal("100") if initial > 0 else Decimal("0")
        )
        equities = [initial, *[pt.equity for pt in equity_curve]]
        returns = [(cur - prev) / prev for prev, cur in pairwise(equities) if prev > 0]
        duration_seconds = ((self._timeline[-1] + interval) - self._timeline[0]).total_seconds()
        periods_per_year = (
            len(equity_curve) * 365 * 24 * 60 * 60 / duration_seconds
            if duration_seconds > 0
            else 0.0
        )
        mdd = maximum_drawdown(equities) if equities else Decimal("0")
        sr = sharpe_ratio(returns, periods_per_year) if len(returns) > 1 else Decimal("0")
        total_wins = sum(1 for t in all_closed if t.pnl > 0)
        total_fees = sum((f.fee for f in all_fills), Decimal("0"))

        summary: dict[str, Any] = {
            "mode": "multi_asset_portfolio_backtest",
            "instruments": list(self.instruments.keys()),
            "started_at": self._timeline[0].isoformat(),
            "completed_at": (self._timeline[-1] + interval).isoformat(),
            "initial_capital": str(initial),
            "final_equity": str(final_equity),
            "net_pnl": str(final_equity - initial),
            "total_return_pct": str(total_ret_pct),
            "maximum_drawdown_pct": str(mdd),
            "sharpe_ratio": str(sr),
            "closed_trade_count": len(all_closed),
            "winning_trade_count": total_wins,
            "win_rate_pct": str(
                (Decimal(total_wins) / Decimal(len(all_closed)) * Decimal("100"))
                if all_closed
                else Decimal("0")
            ),
            "total_fees": str(total_fees),
            "per_instrument": per_inst,
        }
        return MultiAssetBacktestResult(
            run_id=self.run_id,
            strategy_name=self.config.strategy.name,
            instruments=tuple(self.instruments.keys()),
            bar=self.config.market.bar,
            started_at=self._timeline[0],
            completed_at=self._timeline[-1] + interval,
            initial_capital=initial,
            final_equity=final_equity,
            fills=tuple(all_fills),
            closed_trades=tuple(all_closed),
            equity_curve=tuple(equity_curve),
            summary=summary,
            per_instrument_summary=per_inst,
        )

    def _portfolio_equity(
        self,
        *,
        mark_override: dict[str, Decimal] | None = None,
    ) -> Decimal:
        cash = self.portfolio.balances.get(self.quote_currency, Decimal("0"))
        overrides = mark_override or {}
        holdings_val = Decimal("0")
        for inst_id, slot in self._slots.items():
            qty = self.portfolio.positions.get(inst_id, Decimal("0"))
            if qty <= 0:
                continue
            price = overrides.get(inst_id, slot.last_close)
            if price is not None and price > 0:
                holdings_val += qty * price
        return cash + holdings_val
