from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.events import DomainEvent, EventBus
from app.domain.market import Candle, Instrument
from app.domain.order import (
    ApprovedOrder,
    Order,
    OrderSide,
    OrderSource,
    OrderType,
    ProposedOrder,
)
from app.domain.position import PortfolioSnapshot
from app.domain.risk import RiskDecision
from app.domain.signal import Signal, SignalAction
from app.execution.base import Broker
from app.execution.order_manager import new_client_order_id
from app.position_sizing.base import PositionSizeDecision, PositionSizer
from app.risk.risk_manager import RiskContext, RiskManager
from app.runtime.clock import Clock
from app.strategies.base import Strategy


class PortfolioSource(Protocol):
    def snapshot(self) -> PortfolioSnapshot: ...


class EngineRepository(Protocol):
    def save_signal(self, signal: Signal) -> None: ...

    def save_risk_decision(self, signal_id: str, decision: RiskDecision) -> None: ...

    def save_order(self, order: Order) -> None: ...


@dataclass(frozen=True, slots=True)
class ProtectiveExitPolicy:
    enabled: bool
    stop_loss_pct: Decimal
    take_profit_pct: Decimal


@dataclass(frozen=True, slots=True)
class EngineRiskState:
    daily_pnl: Decimal | None
    drawdown_pct: Decimal | None
    recent_order_times: tuple[datetime, ...] = ()
    circuit_broken: bool = False


@dataclass(frozen=True, slots=True)
class EngineExecution:
    signal: Signal
    position_size: PositionSizeDecision
    proposed_order: ProposedOrder
    risk_decision: RiskDecision
    order: Order | None


class TradingEngine:
    """Exchange-independent strategy, sizing, risk and execution pipeline."""

    def __init__(
        self,
        *,
        run_id: str,
        mode: TradingMode,
        bar: str,
        instrument: Instrument,
        strategy: Strategy,
        position_sizer: PositionSizer,
        risk_manager: RiskManager,
        broker: Broker,
        portfolio: PortfolioSource,
        clock: Clock,
        event_bus: EventBus,
        protective_exits: ProtectiveExitPolicy,
        repository: EngineRepository | None = None,
    ) -> None:
        self.run_id = run_id
        self.mode = mode
        self.bar = bar
        self.instrument = instrument
        self.strategy = strategy
        self.position_sizer = position_sizer
        self.risk_manager = risk_manager
        self.broker = broker
        self.portfolio = portfolio
        self.clock = clock
        self.event_bus = event_bus
        self.protective_exits = protective_exits
        self.repository = repository
        self.signal_count = 0
        self.actionable_signal_count = 0
        self.approved_order_count = 0
        self.submitted_order_count = 0

    def start(self) -> None:
        self.strategy.on_start(self._context(None))

    def stop(self) -> None:
        self.strategy.on_stop(self._context(None))

    def generate_signals(self, candle: Candle) -> list[Signal]:
        if not candle.confirmed:
            raise ValueError("交易引擎只接受已确认收盘 K 线")
        self._publish(
            "BarReceived",
            candle.timestamp,
            {"close": str(candle.close), "confirmed": candle.confirmed},
            f"candle:{self.run_id}:{self.instrument.instrument_id}:{self.bar}:"
            f"{candle.timestamp.isoformat()}",
        )
        market = MarketSnapshot(candle, candle.close)
        context = self._context(market)
        signals = self.strategy.on_bar(context, candle)
        protective = self._protective_exit(context, candle)
        if protective is not None:
            signals = [protective]
        for signal in signals:
            self.signal_count += 1
            if signal.action is not SignalAction.HOLD:
                self.actionable_signal_count += 1
            if self.repository is not None:
                self.repository.save_signal(signal)
            self._publish(
                "SignalGenerated",
                signal.timestamp,
                {
                    "signal_id": signal.signal_id,
                    "action": signal.action.value,
                    "reason": signal.reason,
                    "confidence": str(signal.confidence),
                    "metadata": signal.metadata,
                },
                self._signal_key(signal),
            )
        return signals

    def execute_signal(
        self,
        signal: Signal,
        candle: Candle,
        *,
        execution_price: Decimal,
        risk_state: EngineRiskState,
        submit_orders: bool,
    ) -> EngineExecution | None:
        if signal.action is SignalAction.HOLD:
            return None
        price = _quantize_price(execution_price, self.instrument.price_tick)
        market = MarketSnapshot(candle, price)
        snapshot = self.portfolio.snapshot()
        size = self.position_sizer.calculate(signal, snapshot, market, self.instrument)
        self._publish(
            "PositionSizeCalculated",
            self.clock.now(),
            {
                "signal_id": signal.signal_id,
                "quantity": str(size.quantity),
                "notional": str(size.notional),
                "reason": size.reason,
            },
            f"size:{self.run_id}:{signal.signal_id}",
        )
        side = OrderSide.BUY if signal.action is SignalAction.BUY else OrderSide.SELL
        proposed = ProposedOrder(
            client_order_id=new_client_order_id(self.clock),
            run_id=self.run_id,
            strategy_name=self.strategy.name,
            instrument_id=self.instrument.instrument_id,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=size.quantity,
            price=price,
            signal_id=signal.signal_id,
            created_at=self.clock.now(),
            mode=self.mode.value,
            bar=self.bar,
            order_source=self._order_source(signal),
        )
        open_orders = tuple(self.broker.get_open_orders(self.instrument.instrument_id))
        decision = self.risk_manager.evaluate(
            RiskContext(
                mode=self.mode,
                portfolio=snapshot,
                instrument=self.instrument,
                market=market,
                signal=signal,
                now=self.clock.now(),
                open_orders=open_orders,
                open_order_sides=frozenset(order.request.side for order in open_orders),
                recent_order_times=risk_state.recent_order_times,
                daily_pnl=risk_state.daily_pnl,
                drawdown_pct=risk_state.drawdown_pct,
                circuit_broken=risk_state.circuit_broken,
            ),
            proposed,
        )
        if self.repository is not None:
            self.repository.save_risk_decision(signal.signal_id, decision)
        self._publish(
            "RiskApproved" if decision.allowed else "RiskRejected",
            self.clock.now(),
            {
                "signal_id": signal.signal_id,
                "rejected_by": list(decision.rejected_by),
                "reasons": list(decision.reasons),
                "rule_results": [
                    {
                        "rule_name": result.rule_name,
                        "allowed": result.allowed,
                        "reason": result.reason,
                    }
                    for result in decision.rule_results
                ],
                "risk_snapshot": decision.risk_snapshot,
            },
            f"risk:{self.run_id}:{signal.signal_id}",
        )
        order: Order | None = None
        if decision.allowed and decision.adjusted_order is not None:
            self.approved_order_count += 1
            if submit_orders:
                approved = ApprovedOrder(
                    proposed=decision.adjusted_order,
                    approved_at=self.clock.now(),
                    approval_reason=decision.reasons[0],
                )
                order = self.broker.submit_order(approved)
                self.submitted_order_count += 1
                if self.repository is not None:
                    self.repository.save_order(order)
                self.strategy.on_order_update(self._context(market), order)
                self._publish(
                    "OrderEvent",
                    self.clock.now(),
                    {
                        "client_order_id": order.request.client_order_id,
                        "state": order.state.value,
                        "filled_quantity": str(order.filled_quantity),
                        "average_price": str(order.average_price),
                    },
                    f"order:{order.request.client_order_id}:{order.state.value}",
                )
        return EngineExecution(signal, size, proposed, decision, order)

    def _order_source(self, signal: Signal) -> OrderSource:
        if self.mode is TradingMode.BACKTEST:
            return OrderSource.BACKTEST
        if bool(signal.metadata.get("manual_demo_order")):
            return OrderSource.MANUAL_DEMO_TEST
        if bool(signal.metadata.get("protective_exit")):
            return OrderSource.PROTECTIVE_EXIT
        return OrderSource.STRATEGY_DEMO

    def _protective_exit(self, context: StrategyContext, candle: Candle) -> Signal | None:
        if not self.protective_exits.enabled:
            return None
        position = context.portfolio_snapshot.position(self.instrument.instrument_id)
        cost = context.portfolio_snapshot.position_cost(self.instrument.instrument_id)
        entry = cost.average_entry_price
        if position <= 0 or not cost.cost_is_reliable or entry is None:
            return None
        stop = entry * (Decimal("1") - self.protective_exits.stop_loss_pct / Decimal("100"))
        take = entry * (Decimal("1") + self.protective_exits.take_profit_pct / Decimal("100"))
        if candle.close <= stop:
            reason = "触发止损"
        elif candle.close >= take:
            reason = "触发止盈"
        else:
            return None
        identity = (
            f"{self.run_id}:{self.strategy.name}:{self.instrument.instrument_id}:"
            f"{candle.timestamp.isoformat()}:{reason}"
        )
        return Signal(
            signal_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
            strategy_name=self.strategy.name,
            instrument_id=self.instrument.instrument_id,
            action=SignalAction.CLOSE,
            timestamp=self.clock.now(),
            reason=reason,
            confidence=Decimal("1"),
            metadata={
                "candle_timestamp": candle.timestamp.isoformat(),
                "candle_confirmed": candle.confirmed,
                "protective_exit": True,
            },
        )

    def _context(self, market: MarketSnapshot | None) -> StrategyContext:
        return StrategyContext(
            run_id=self.run_id,
            mode=self.mode,
            strategy_name=self.strategy.name,
            instrument=self.instrument,
            bar=self.bar,
            portfolio_snapshot=self.portfolio.snapshot(),
            market_snapshot=market,
            clock=self.clock,
        )

    def _publish(
        self,
        event_type: str,
        timestamp: datetime,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> None:
        self.event_bus.publish(
            DomainEvent(
                run_id=self.run_id,
                timestamp=timestamp,
                event_type=event_type,
                instrument_id=self.instrument.instrument_id,
                strategy_name=self.strategy.name,
                payload=payload,
                idempotency_key=idempotency_key,
            )
        )

    def _signal_key(self, signal: Signal) -> str:
        return (
            f"signal:{self.run_id}:{signal.strategy_name}:{signal.instrument_id}:"
            f"{signal.timestamp.isoformat()}:{signal.action.value}"
        )


def _quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
