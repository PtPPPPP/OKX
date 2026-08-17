from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from app.domain.market import Candle
from app.domain.position import PortfolioSnapshot
from app.domain.risk import RiskDecision
from app.domain.signal import Signal, SignalAction
from app.market.historical_data import BAR_INTERVALS
from app.market.websocket import OKXPublicWebSocketProvider
from app.runtime.clock import BacktestClock
from app.trading_engine import EngineRiskState, TradingEngine


@dataclass(frozen=True, slots=True)
class DemoEvaluation:
    signal: Signal
    risk_decision: RiskDecision | None


@dataclass(frozen=True, slots=True)
class DemoEvaluationResult:
    run_id: str
    evaluations: tuple[DemoEvaluation, ...]
    submitted_order: bool = False


class StaticPortfolioSource:
    def __init__(self, portfolio: PortfolioSnapshot) -> None:
        self.portfolio = portfolio

    def snapshot(self) -> PortfolioSnapshot:
        return self.portfolio


class DemoEvaluationRunner:
    def __init__(
        self,
        *,
        run_id: str,
        bar: str,
        candles: list[Candle],
        engine: TradingEngine,
        clock: BacktestClock,
        daily_pnl: Decimal | None,
        drawdown_pct: Decimal | None,
    ) -> None:
        self.run_id = run_id
        self.bar = bar
        self.candles = candles
        self.engine = engine
        self.clock = clock
        self.daily_pnl = daily_pnl
        self.drawdown_pct = drawdown_pct

    def run(self) -> DemoEvaluationResult:
        interval = BAR_INTERVALS.get(self.bar.lower())
        if interval is None:
            raise ValueError(f"不支持的 K 线周期: {self.bar}")
        if not self.candles:
            raise ValueError("观察模式没有已确认收盘 K 线")
        evaluations: list[DemoEvaluation] = []
        self.engine.start()
        try:
            for candle in self.candles:
                self.clock.advance_to(candle.timestamp + interval)
                signals = self.engine.generate_signals(candle)
                for signal in signals:
                    execution = None
                    if signal.action is not SignalAction.HOLD:
                        execution = self.engine.execute_signal(
                            signal,
                            candle,
                            execution_price=candle.close,
                            risk_state=EngineRiskState(
                                daily_pnl=self.daily_pnl,
                                drawdown_pct=self.drawdown_pct,
                            ),
                            submit_orders=False,
                        )
                    evaluations.append(
                        DemoEvaluation(
                            signal,
                            execution.risk_decision if execution is not None else None,
                        )
                    )
        finally:
            self.engine.stop()
        return DemoEvaluationResult(self.run_id, tuple(evaluations))


@dataclass(frozen=True, slots=True)
class PublicObserveResult:
    run_id: str
    evaluations: tuple[DemoEvaluation, ...]
    confirmed_candle_count: int
    timed_out: bool
    connected: bool
    connection_state: str
    reconnect_count: int
    last_error: str | None
    submitted_order: bool = False


class PublicObserveRunner:
    def __init__(
        self,
        *,
        run_id: str,
        instrument_id: str,
        bar: str,
        engine: TradingEngine,
        clock: BacktestClock,
        provider: OKXPublicWebSocketProvider,
        max_events: int,
        timeout_seconds: float,
    ) -> None:
        if max_events <= 0:
            raise ValueError("max_events 必须大于 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.run_id = run_id
        self.instrument_id = instrument_id
        self.bar = bar
        self.engine = engine
        self.clock = clock
        self.provider = provider
        self.max_events = max_events
        self.timeout_seconds = timeout_seconds

    def run(self) -> PublicObserveResult:
        return asyncio.run(self._run_async())

    async def _run_async(self) -> PublicObserveResult:
        interval = BAR_INTERVALS.get(self.bar.lower())
        if interval is None:
            raise ValueError(f"不支持的 K 线周期: {self.bar}")
        evaluations: list[DemoEvaluation] = []
        candle_count = 0
        timed_out = False
        self.engine.start()
        try:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    async for candle in self.provider.stream_confirmed_candles(
                        self.instrument_id, self.bar
                    ):
                        self.clock.advance_to(candle.timestamp + interval)
                        signals = self.engine.generate_signals(candle)
                        for signal in signals:
                            execution = None
                            if signal.action is not SignalAction.HOLD:
                                execution = self.engine.execute_signal(
                                    signal,
                                    candle,
                                    execution_price=candle.close,
                                    risk_state=EngineRiskState(
                                        daily_pnl=None,
                                        drawdown_pct=None,
                                    ),
                                    submit_orders=False,
                                )
                            evaluations.append(
                                DemoEvaluation(
                                    signal,
                                    execution.risk_decision if execution is not None else None,
                                )
                            )
                        candle_count += 1
                        if candle_count >= self.max_events:
                            break
            except TimeoutError:
                timed_out = True
        finally:
            self.engine.stop()
            await self.provider.stop()
        return PublicObserveResult(
            run_id=self.run_id,
            evaluations=tuple(evaluations),
            confirmed_candle_count=candle_count,
            timed_out=timed_out,
            connected=self.provider.connected_once,
            connection_state=self.provider.state.value,
            reconnect_count=self.provider.reconnect_count,
            last_error=self.provider.last_error,
        )
